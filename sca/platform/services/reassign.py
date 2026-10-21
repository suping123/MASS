"""有条件换承运商。

为什么以前完全禁止，现在改成"有条件允许"
----------------------------------------
原规则是"非必要不换"——异常处置只能改路线/改时间。这条规则挡住了绝大多数被滥用
的场景（每次出问题就换人，货主权益没有保障）。但现实里确实存在**不换就没法继续**
的情况：车报废了、司机失联了、复核发现没有冷链资质。这些不是"要不要换"的问题，
是"什么时候换、谁批准、责任怎么算"的问题。

所以这里把"禁止"改成 **5 个可枚举触发码 + 强制原因 + 人工审批**：
触发码在代码里是常量，不满足条件直接拒绝，不给自由裁量的空间。

成本责任（本轮口径，待业务确认）
--------------------------------
**不动 settlement**，只在事件里记 `cost_bearer`。理由：平台现在没有真实 GPS 里程，
按"已行驶里程给原承运商结算"只能编数。钱的归属走线下，系统只负责把事实记清楚。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (CarrierBid, CarrierProfile, ExceptionReport, OrderEvent,
                      OrderPublish, ShipperOrder, SysOrg)
from . import audit, notify
from .orders import STATUS_LABEL, transition

# 每个触发码的判定条件与说明。放到一个表里，是为了让"什么时候允许换人"
# 变成可读、可测、可审计的规则，而不是散落在 if 里的隐式判断。
REASSIGN_TRIGGERS: Dict[str, Dict[str, Any]] = {
    "carrier_reject": {
        "label": "承运商拒单/无法承运",
        "auto": False,
        "detail": "承运商明确表示不能跑，需重新找人",
    },
    "carrier_unreachable": {
        "label": "承运商失联",
        "auto": False,
        "max_silent_h": 6,
        "detail": "位置心跳超过 6 小时未更新，视为失联",
    },
    "qualification_mismatch": {
        "label": "资质不符",
        "auto": False,
        "detail": "复核发现承运商不具备该货类所需资质（温控/危品）",
    },
    "vehicle_total_failure": {
        "label": "车辆报废/重大事故",
        "auto": False,
        "codes": ("E-COLD-FAIL", "E-ACCIDENT", "E-DG-LEAK", "E-DG-OVER"),
        "detail": "车辆或货物已无法继续运输，改路线解决不了",
    },
    "manual_force": {
        "label": "调度强制改派",
        "auto": False,
        "min_reason": 10,
        "detail": "无技术依据，调度人工判断（需写明具体理由）",
    },
}

REASSIGNABLE = ("claimed", "in_transit", "exception")


class ReassignError(ValueError):
    """不满足改派条件。路由层转 409。"""


def _profile(db: Session, org_id: Optional[int]) -> Optional[CarrierProfile]:
    if not org_id:
        return None
    return db.execute(select(CarrierProfile).where(
        CarrierProfile.org_id == int(org_id))).scalars().first()


def check_eligible(db: Session, order: ShipperOrder,
                   reason_code: str) -> Tuple[bool, str]:
    """判定这笔订单能否以该触发码改派。返回 (是否允许, 原因)。"""
    if reason_code not in REASSIGN_TRIGGERS:
        return False, (f"未知改派触发码「{reason_code}」，合法值："
                       + " / ".join(REASSIGN_TRIGGERS))
    if order.status not in REASSIGNABLE:
        return False, (f"订单当前状态「{STATUS_LABEL.get(order.status, order.status)}」"
                       f"不允许改派（仅 已接单/在途/异常 可改派）")
    if not order.carrier_org_id:
        return False, "该订单还没有承运商，谈不上改派"

    t = REASSIGN_TRIGGERS[reason_code]

    if reason_code == "carrier_unreachable":
        p = _profile(db, order.carrier_org_id)
        limit_h = float(t["max_silent_h"])
        if not p or not p.position_updated_at:
            return False, "承运商没有位置心跳记录，无法判定失联（先让其上报位置）"
        silent_h = (datetime.now() - p.position_updated_at).total_seconds() / 3600
        if silent_h < limit_h:
            return False, (f"承运商 {silent_h:.1f} 小时前还有位置更新，"
                           f"未达失联阈值 {limit_h:.0f} 小时")
        return True, f"已失联 {silent_h:.1f} 小时"

    if reason_code == "qualification_mismatch":
        if not (order.temp_ctrl or order.hazmat or
                (order.cargo_type or "") in ("医药冷链", "生鲜冷链", "危险品")):
            return False, "该订单不是温控/危险品货类，不构成资质不符"
        return True, "温控/危险品货类需具备相应资质"

    if reason_code == "vehicle_total_failure":
        rows = db.execute(select(ExceptionReport).where(
            ExceptionReport.order_id == order.id,
            ExceptionReport.status != "resolved")).scalars().all()
        hit = [e for e in rows if (e.exception_code or "") in t["codes"]]
        if not hit:
            return False, ("该订单没有「车辆报废/重大事故」类未处置异常，"
                           "不满足强制改派条件")
        return True, f"命中异常 {hit[0].exception_code}（{hit[0].exception_name or ''}）"

    if reason_code == "manual_force":
        return True, "人工强制改派（需写明理由）"

    return True, t["detail"]


def reassign(db: Session, order: ShipperOrder, *,
             reason_code: str, reason: str, user: Dict[str, Any],
             new_carrier_org_id: Optional[int] = None,
             ip: str = "") -> Dict[str, Any]:
    """执行改派。释放原承运商 → 直接指派或退回挂牌 → 留痕 → 通知三方。"""
    ok, why = check_eligible(db, order, reason_code)
    if not ok:
        raise ReassignError(why)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ReassignError("改派原因至少 10 个字（这是要写进审计日志的责任依据）")

    if new_carrier_org_id:
        org = db.get(SysOrg, int(new_carrier_org_id))
        if not org:
            raise ReassignError("指定的新承运商不存在")
        if int(new_carrier_org_id) == int(order.carrier_org_id or 0):
            raise ReassignError("新承运商与原承运商相同，不需要改派")
        if not _profile(db, new_carrier_org_id):
            raise ReassignError("新承运商没有档案（cost_per_km / 载重），无法承接")

    from_org = order.carrier_org_id
    from_user = order.carrier_user_id
    status_before = order.status

    # ① 释放原承运商：历史 bid 不删，标记 withdraw，便于事后追责
    for b in db.execute(select(CarrierBid).where(
            CarrierBid.order_id == order.id,
            CarrierBid.decision.in_(("accepted", "candidate")),
            CarrierBid.carrier_org_id == from_org)).scalars().all():
        b.decision = "withdrawn"
        b.decided_at = datetime.now()

    # ② 关闭在挂的挂牌单（若回池会另起一条）
    pubs = db.execute(select(OrderPublish).where(
        OrderPublish.order_id == order.id,
        OrderPublish.status == "open")).scalars().all()
    for p in pubs:
        p.status = "cancelled"
        p.closed_at = datetime.now()

    order.carrier_org_id = None
    order.carrier_user_id = None
    mode = "pool"
    new_publish_id: Optional[int] = None
    to_org: Optional[int] = None

    if new_carrier_org_id:
        # 直接指派：状态保持（在途的仍在途，换了车而已）
        to_org = int(new_carrier_org_id)
        prof = _profile(db, to_org)
        order.carrier_org_id = to_org
        order.carrier_user_id = prof.carrier_user_id if prof else None
        db.add(CarrierBid(order_id=order.id,
                          publish_id=pubs[0].id if pubs else 0,
                          carrier_org_id=to_org,
                          carrier_user_id=order.carrier_user_id,
                          decision="accepted", decided_at=datetime.now()))
        mode = "direct"
    else:
        # 回挂牌池重新找人：这条状态转移是 reassign 专用（见 orders.ALLOWED 注释）
        transition(db, order, "published", "order_reassigned_to_pool", "dispatcher",
                   user.get("user_id"), {"reason_code": reason_code, "reason": reason})
        plan_price = None
        if pubs:
            plan_price = pubs[0].publish_price
        else:
            last = db.execute(select(OrderPublish).where(
                OrderPublish.order_id == order.id).order_by(
                OrderPublish.id.desc())).scalars().first()
            plan_price = last.publish_price if last else None
        if plan_price and order.active_plan_id:
            pub = OrderPublish(order_id=order.id, plan_id=order.active_plan_id,
                               gross_price=plan_price, shipper_fee_rate=0,
                               shipper_fee=0, carrier_fee_rate=0,
                               publish_price=plan_price, status="open",
                               published_by=user.get("user_id"))
            db.add(pub)
            db.flush()
            new_publish_id = pub.id

    seq = (db.execute(select(func.max(OrderEvent.seq)).where(
        OrderEvent.order_id == order.id)).scalar() or 0) + 1
    payload = {"reason_code": reason_code,
               "reason_label": REASSIGN_TRIGGERS[reason_code]["label"],
               "reason": reason, "from_org_id": from_org, "to_org_id": to_org,
               "mode": mode, "new_publish_id": new_publish_id,
               "settlement_unchanged": True,
               # 本轮不动结算：钱怎么分是业务决策，系统只记责任方，别装作算过
               "cost_bearer": "pending_manual",
               "status_from": status_before, "status_to": order.status}
    db.add(OrderEvent(order_id=order.id, seq=seq, event_type="order_reassigned",
                      actor_role=user.get("role") or "dispatcher",
                      actor_user_id=user.get("user_id"), payload=payload))

    # ③ 通知三方：原承运商要知道自己被撤了，货主要知道换了人
    if from_user:
        notify.push(db, from_user, ntype="reassigned",
                    title=f"订单 {order.order_no} 已被改派",
                    body=f"原因：{REASSIGN_TRIGGERS[reason_code]['label']}\n"
                         f"说明：{reason}",
                    order_id=order.id)
    notify.push_order(db, order, ntype="reassigned",
                      title=f"订单 {order.order_no} 承运商已变更",
                      body=f"原承运商已撤出（{REASSIGN_TRIGGERS[reason_code]['label']}），"
                           f"当前状态：{STATUS_LABEL.get(order.status, order.status)}",
                      to_shipper=True, to_carrier=bool(to_org))

    audit.log(db, user, "order_reassigned", target_type="order", target_id=order.id,
              reason=reason, detail={"order_no": order.order_no,
                                     "reason_code": reason_code,
                                     "from_org_id": from_org, "to_org_id": to_org,
                                     "mode": mode}, ip=ip)
    db.commit()
    return {"order_id": order.id, "order_no": order.order_no,
            "reason_code": reason_code, "mode": mode,
            "from_org_id": from_org, "to_org_id": to_org,
            "status": order.status, "new_publish_id": new_publish_id,
            "settlement_unchanged": True,
            "note": (f"已改派（{REASSIGN_TRIGGERS[reason_code]['label']}）："
                     + ("已指定新承运商，订单保持原状态"
                        if mode == "direct" else "已退回挂牌池，等待重新接单")
                     + "；结算未变动，费用归属待人工确认")}


__all__ = ["REASSIGN_TRIGGERS", "REASSIGNABLE", "ReassignError",
           "check_eligible", "reassign"]
