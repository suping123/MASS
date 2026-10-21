"""订单主数据服务：状态机 + 事件流。

两条纪律（违反会导致三端状态打架）：
  1) 任何状态变更必须走 `transition()`，不许在别处直接改 `order.status`；
  2) 每次变更必须先写 `order_event`，事件是平台间信息流动的唯一通道。

seq 用"锁订单行 + MAX+1"生成，配合 `uk_event_seq` 唯一键兜底：
并发时最坏情况是被唯一键拒绝而不是写出重复序号——宁可报错也不能重放。
"""
from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import OrderEvent, ShipperOrder

# 合法状态转移。做白名单而不是黑名单：新增状态忘了登记就会立刻抛错，
# 而不是静默地允许一个不该发生的跳转。
ALLOWED: Dict[str, set] = {
    "draft":       {"submitted", "cancelled"},
    "submitted":   {"planned", "cancelled"},
    "planned":     {"published", "cancelled"},
    "published":   {"candidate", "cancelled"},
    "candidate":   {"claimed", "published", "cancelled"},   # 撤回候选 -> 回到挂牌
    # claimed -> exception：接单后还没发车就出事（车坏了/装不上货）是真实场景，
    # 不能逼着承运商先"发车"再报异常
    "claimed":     {"in_transit", "exception", "cancelled", "published"},
    # in_transit -> published：有条件改派（拒单/失联/资质不符/车辆报废/人工强制）
    # 时订单要回到挂牌池重新找人跑，这是唯一允许"回退到挂牌"的正规路径，
    # 且必须经 services/reassign.py 的条件校验与人工审批。
    "in_transit":  {"exception", "delivered", "published"},
    "exception":   {"in_transit", "delivered", "cancelled", "published"},
    "delivered":   {"settled"},
    "settled":     set(),
    "cancelled":   set(),
}

STATUS_LABEL = {
    "draft": "草稿（对话中）",
    "submitted": "已提交，待调度",
    "planned": "已出方案，待发布",
    "published": "已挂牌，待承运商接单",
    "candidate": "有承运商意向",
    "claimed": "承运商已接单",
    "in_transit": "在途",
    "exception": "异常处置中",
    "delivered": "已送达",
    "settled": "已结算",
    "cancelled": "已取消",
}


def gen_order_no() -> str:
    """订单号：SO + 年月日 + 6 位随机。唯一键兜底，不依赖数据库自增对外暴露。"""
    return f"SO{datetime.now():%Y%m%d}{secrets.randbelow(1_000_000):06d}"


def create_order(db: Session, user: Dict[str, Any],
                 slots: Dict[str, Any]) -> Dict[str, Any]:
    """落单（唯一入口）。

    为什么从货主 agent 的对话状态机里搬出来：S5 之后**执行者是 shipper 常驻进程**，
    对话那边只负责"投消息 + 等回复"。如果还留在 agent 里，就会出现两份落单逻辑，
    "对话下的单"和"进程下的单"迟早长歪（字段少一个、事件多一条）。

    ``slots`` 是槽位字典（可能是对话抽出来的，也可能来自页面表单）。
    文本与绝对时间都落库：时效判断、改期、SLA 都直接用绝对时间，
    不靠每次重新解析"后天下午六点前"。
    """
    from .deadline import parse_required_by

    order = ShipperOrder(
        order_no=gen_order_no(),
        shipper_user_id=user["user_id"],
        shipper_org_id=user.get("org_id"),
        source_text=str(slots.get("_raw") or "")[:2000],
        origin=str(slots.get("origin") or "")[:64] or None,
        dest=str(slots.get("dest") or "")[:64] or None,
        cargo_type=str(slots.get("cargo_type") or "普货")[:32],
        weight_t=_dec_of(slots.get("weight_t")),
        temp_ctrl=bool(slots.get("temp_ctrl")),
        hazmat=bool(slots.get("hazmat")),
        required_by=str(slots.get("required_by") or "")[:64] or None,
        required_by_at=_parse_required_by_safe(parse_required_by, slots.get("required_by")),
        budget_max=_dec_of(slots.get("budget_max")),
        contact_phone=str(slots.get("contact_phone") or "")[:32] or None,
        status="draft",
    )
    db.add(order)
    db.flush()

    add_event(db, order.id, "order_created", "shipper", user["user_id"], {
        "order_no": order.order_no, "origin": order.origin, "dest": order.dest,
        "cargo_type": order.cargo_type,
        "weight_t": float(order.weight_t) if order.weight_t is not None else None,
    })
    transition(db, order, "submitted", "order_submitted", "shipper",
               user["user_id"], {"budget_max": float(order.budget_max)
                                 if order.budget_max is not None else None})
    db.commit()
    return {"order_id": order.id, "order_no": order.order_no,
            "status": order.status, "status_label": STATUS_LABEL.get(order.status),
            "origin": order.origin, "dest": order.dest,
            "cargo_type": order.cargo_type,
            "weight_t": float(order.weight_t) if order.weight_t is not None else None,
            "budget_max": float(order.budget_max) if order.budget_max is not None else None,
            "required_by": order.required_by,
            "contact_phone": order.contact_phone}


def _dec_of(v: Any) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (ArithmeticError, ValueError):
        return None


def _parse_required_by_safe(fn, text: Any):
    """解析"后天下午6点前"；解析不出来返回 None，不硬凑一个时间。"""
    try:
        return fn(str(text or ""))
    except Exception:                           # noqa: BLE001 解析失败不阻塞落单
        return None


def add_event(db: Session, order_id: int, event_type: str, actor_role: str,
              actor_user_id: Optional[int] = None,
              payload: Optional[Dict[str, Any]] = None) -> OrderEvent:
    """追加事件。对订单行加锁以保证同一订单的 seq 不重号。"""
    db.execute(select(ShipperOrder.id).where(
        ShipperOrder.id == order_id).with_for_update())
    cur = db.execute(select(func.coalesce(func.max(OrderEvent.seq), 0)).where(
        OrderEvent.order_id == order_id)).scalar_one()
    ev = OrderEvent(order_id=order_id, seq=int(cur) + 1, event_type=event_type,
                    actor_role=actor_role, actor_user_id=actor_user_id,
                    payload=_jsonable(payload or {}))
    db.add(ev)
    db.flush()
    return ev


def transition(db: Session, order: ShipperOrder, to_status: str, event_type: str,
               actor_role: str, actor_user_id: Optional[int] = None,
               payload: Optional[Dict[str, Any]] = None) -> ShipperOrder:
    """推进订单状态并写事件。非法转移直接抛 ValueError（由路由转 409）。"""
    frm = order.status
    if to_status == frm:
        add_event(db, order.id, event_type, actor_role, actor_user_id,
                  {**(payload or {}), "note": "状态未变（幂等）"})
        return order
    if to_status not in ALLOWED.get(frm, set()):
        raise ValueError(f"订单状态不能从「{STATUS_LABEL.get(frm, frm)}」"
                         f"变为「{STATUS_LABEL.get(to_status, to_status)}」")
    order.status = to_status
    order.updated_at = datetime.now()
    db.flush()
    add_event(db, order.id, event_type, actor_role, actor_user_id,
              {**(payload or {}), "from": frm, "to": to_status})
    return order


def _jsonable(v: Any) -> Any:
    """Decimal / datetime 不能直接进 JSON 列，统一降级为可序列化类型。"""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def order_brief(o: ShipperOrder) -> Dict[str, Any]:
    """列表视图：不带预算（承运商不该看到）也不带内部字段。"""
    return {
        "id": o.id, "order_no": o.order_no,
        "origin": o.origin, "dest": o.dest,
        "cargo_type": o.cargo_type,
        "weight_t": float(o.weight_t) if o.weight_t is not None else None,
        "temp_ctrl": bool(o.temp_ctrl), "hazmat": bool(o.hazmat),
        "required_by": o.required_by,
        "status": o.status, "status_label": STATUS_LABEL.get(o.status, o.status),
        "created_at": o.created_at.isoformat(timespec="seconds") if o.created_at else None,
    }


def order_detail(o: ShipperOrder, *, include_budget: bool = True) -> Dict[str, Any]:
    d = order_brief(o)
    d.update({
        "source_text": o.source_text,
        "contact_name": o.contact_name, "contact_phone": o.contact_phone,
        "carrier_org_id": o.carrier_org_id,
        "active_plan_id": o.active_plan_id,
        # 承诺时效的绝对时间。`required_by` 是货主原话（"后天下午6点前"这种），
        # 调度改期时必须给得出"从几点改到几点"，只靠原话算不出来。
        "required_by_at": (o.required_by_at.isoformat(timespec="minutes")
                           if o.required_by_at else None),
    })
    if include_budget:
        d["budget_max"] = float(o.budget_max) if o.budget_max is not None else None
    return d


# 可取消的状态：未完成的都可以取消。
# 刻意不含 draft（还没提交，直接丢弃即可，不必留取消记录）与
# delivered / settled（货已交付/已结算，取消会把账做坏——要退货得走单独的售后流程）。
CANCELLABLE = {"submitted", "planned", "published", "candidate",
               "claimed", "in_transit", "exception"}


def cancel_order(db: Session, order: ShipperOrder, actor: Dict[str, Any],
                 reason: str, ip: str = "") -> Dict[str, Any]:
    """取消订单：释放承运商、关闭挂牌、写事件与操作日志，一次事务内完成。

    归属校验由调用方（各端路由）负责——服务层只管"这单能不能取消"。
    """
    from ..models import CarrierBid, OrderPublish
    from . import audit

    if order.status not in CANCELLABLE:
        raise ValueError(f"订单当前状态「{STATUS_LABEL.get(order.status, order.status)}」不能取消")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ValueError("请填写取消原因（至少 2 个字）")

    prev = order.status
    released = None

    # 已接单的承运商要释放：否则这单在承运商端会一直显示"在途"，
    # 而调度端已经取消，两边状态打架。
    if order.carrier_org_id:
        released = order.carrier_org_id
        for b in db.execute(select(CarrierBid).where(
                CarrierBid.order_id == order.id,
                CarrierBid.decision.in_(("accepted", "candidate")))).scalars().all():
            b.decision = "withdrawn"

    # 挂牌要下线，否则公共池里还挂着一个已取消的单
    for pub in db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id,
            OrderPublish.status == "open")).scalars().all():
        pub.status = "cancelled"
        pub.closed_at = datetime.now()

    order.carrier_org_id = None
    order.carrier_user_id = None

    transition(db, order, "cancelled", "order_cancelled", actor.get("role", "system"),
               actor.get("user_id"), {"reason": reason, "from": prev,
                                      "released_carrier_org_id": released})
    audit.log(db, actor, "order_cancelled", target_type="order", target_id=order.id,
              reason=reason,
              detail={"order_no": order.order_no, "from_status": prev,
                      "released_carrier_org_id": released}, ip=ip)
    db.commit()
    return {"order_id": order.id, "order_no": order.order_no,
            "from_status": prev, "status": order.status,
            "released_carrier_org_id": released, "reason": reason}


# 回退白名单：只允许"退回上一个业务节点"，不允许跨级跳。
# 刻意不放进 ALLOWED：正常流转是单向的，回退是运维动作，必须走单独入口并留痕；
# 若把回退混进状态机，任何代码路径都能"倒车"，状态机就失去约束力了。
ROLLBACK = {
    "exception": {"in_transit"},   # 异常误报/自行恢复 -> 退回在途
    "published": {"planned"},      # 撤下挂牌 -> 重新定价
    "candidate": {"published"},    # 候选人全撤 -> 回到公开池
    "claimed":   {"published"},    # 承运商无法承运 -> 退回重发（不换承运商时用它）
}


def rollback_order(db: Session, order: ShipperOrder, actor: Dict[str, Any],
                   target: str, reason: str, ip: str = "") -> Dict[str, Any]:
    """把订单回退到上一个业务节点（人工审批驳回 / 审核驳回后的返工入口）。"""
    from ..models import ExceptionReport
    from . import audit

    allowed = ROLLBACK.get(order.status, set())
    if target not in allowed:
        raise ValueError(f"「{STATUS_LABEL.get(order.status, order.status)}」不允许回退到"
                         f"「{STATUS_LABEL.get(target, target)}」")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ValueError("请填写回退原因（至少 2 个字）")

    frm = order.status
    order.status = target
    order.updated_at = datetime.now()

    # 从异常态回退，要把还挂着的异常单一起收口，否则调度台会一直显示"待处置"
    if frm == "exception":
        for e in db.execute(select(ExceptionReport).where(
                ExceptionReport.order_id == order.id,
                ExceptionReport.status != "resolved")).scalars().all():
            e.status = "resolved"
            e.resolution = f"回退重处理：{reason}"
            e.resolved_by_user_id = actor.get("user_id")
            e.resolved_at = datetime.now()

    add_event(db, order.id, "order_rolled_back", actor.get("role", "system"),
              actor.get("user_id"), {"from": frm, "to": target, "reason": reason})
    audit.log(db, actor, "order_rolled_back", target_type="order", target_id=order.id,
              reason=reason, detail={"order_no": order.order_no,
                                     "from_status": frm, "to_status": target}, ip=ip)
    db.commit()
    return {"order_id": order.id, "order_no": order.order_no,
            "from_status": frm, "status": target, "reason": reason}
