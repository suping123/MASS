"""承运商侧业务：看池子、试算收益、接单、报异常。

三条口径：
  1) 池子里**绝不出现** `budget_max`（货主预算属货主私有信息）；
  2) 收益算的是"这单接了我能挣多少"，不是"平台抽了多少"——承运商只关心前者；
  3) 接单用订单行锁 + 状态校验做互斥，同一单只能被一个承运商拿下。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (CarrierBid, CarrierProfile, ExceptionReport, OrderPlan,
                      OrderPublish, ShipperOrder, Tender)
from . import pricing, routing
from .orders import STATUS_LABEL, transition


def profile_of(db: Session, org_id: Optional[int]) -> Optional[CarrierProfile]:
    if not org_id:
        return None
    return db.execute(select(CarrierProfile).where(
        CarrierProfile.org_id == org_id)).scalars().first()


def pool(db: Session, org_id: Optional[int] = None,
         origin: Optional[str] = None, dest: Optional[str] = None,
         limit: int = 100) -> List[Dict[str, Any]]:
    """公共订单池：只返回挂牌中的单，且不含货主预算。"""
    q = (select(OrderPublish, ShipperOrder, OrderPlan)
         .join(ShipperOrder, ShipperOrder.id == OrderPublish.order_id)
         .join(OrderPlan, OrderPlan.id == OrderPublish.plan_id)
         .where(OrderPublish.status == "open",
                ShipperOrder.status.in_(("published", "candidate")))
         .order_by(OrderPublish.id.desc()).limit(limit))
    if origin:
        q = q.where(ShipperOrder.origin.like(f"%{origin}%"))
    if dest:
        q = q.where(ShipperOrder.dest.like(f"%{dest}%"))

    prof = profile_of(db, org_id)
    rows = db.execute(q).all()
    # 招标状态一次查完：池子里逐单查 tender 会变成 N+1（100 条 = 100 次往返）
    tenders = {}
    if rows:
        for t in db.execute(select(Tender).where(
                Tender.order_id.in_([o.id for _, o, _ in rows]),
                Tender.status.in_(("open", "negotiating")))).scalars().all():
            tenders[t.order_id] = t
    # 我报过价的单也要一次查完：池子里若只有"报价"按钮，承运商会重复报同一单
    mine: Dict[int, CarrierBid] = {}
    if rows and org_id:
        for b in db.execute(select(CarrierBid).where(
                CarrierBid.order_id.in_([o.id for _, o, _ in rows]),
                CarrierBid.carrier_org_id == int(org_id),
                CarrierBid.quote_amount.isnot(None))).scalars().all():
            mine[b.order_id] = b
    out = []
    for pub, o, plan in rows:
        item = {
            "order_id": o.id, "order_no": o.order_no,
            "origin": o.origin, "dest": o.dest, "cargo_type": o.cargo_type,
            "weight_t": float(o.weight_t) if o.weight_t is not None else None,
            "temp_ctrl": bool(o.temp_ctrl), "hazmat": bool(o.hazmat),
            "required_by": o.required_by,
            "distance_km": float(plan.distance_km) if plan.distance_km else None,
            "eta_h": float(plan.eta_h) if plan.eta_h else None,
            "publish_price": float(pub.publish_price),   # 承运商可见的运费
            "status": o.status,
        }
        t = tenders.get(o.id)
        item["tender_open"] = bool(t)
        item["tender_deadline_at"] = (t.deadline_at.isoformat(timespec="seconds")
                                      if t and t.deadline_at else None)
        b = mine.get(o.id)
        item["my_bid"] = ({"quote": float(b.quote_amount),
                           "eta_h": float(b.quote_eta_h or 0),
                           "round": int(b.bid_round or 1),
                           "decision": b.decision}
                          if b else None)
        if prof and plan.distance_km:
            # 空驶：当前位置 -> 该单起运地。位置解析有缓存，列表里重复调用不会拖慢
            dh = routing.deadhead(prof.current_position, o.origin)
            p = pricing.carrier_profit(pub.publish_price, plan.distance_km,
                                       prof.cost_per_km, pub.carrier_fee_rate,
                                       dh.get("deadhead_km") or 0)
            item["quick_profit"] = float(p["profit"])
            item["deadhead_km"] = float(p["deadhead_km"])
            item["deadhead_cost"] = float(p["deadhead_cost"])
            item["deadhead_note"] = "" if dh.get("ok") else dh.get("reason", "")
        out.append(item)
    return out


def my_bids(db: Session, user: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    """承运商自己的竞价列表：我报了多少、现在第几轮、有没有在跟我谈、结果如何。

    别家的报价一律不出现，只给"有几家在争"——竞价规则要求知道有竞争，
    不要求知道竞争者的价码，否则后报的人照着压价，竞价就成了抢尾盘。
    """
    from .tender_ops import TENDER_STATUS_LABEL, negotiation_of

    org = user.get("org_id")
    if not org:
        return []
    rows = db.execute(
        select(CarrierBid, ShipperOrder)
        .join(ShipperOrder, ShipperOrder.id == CarrierBid.order_id)
        .where(CarrierBid.carrier_org_id == int(org),
               CarrierBid.quote_amount.isnot(None))
        .order_by(CarrierBid.id.desc()).limit(max(1, min(int(limit), 200)))).all()
    if not rows:
        return []
    oids = [o.id for _, o in rows]

    latest: Dict[int, Tender] = {}
    for t in db.execute(select(Tender).where(
            Tender.order_id.in_(oids)).order_by(Tender.id)).scalars().all():
        latest[t.order_id] = t                      # 后写的覆盖先写的 → 取最新一版

    # 每单有几家在报（只给数量）。一次查完，不按单循环。
    counts: Dict[int, int] = {}
    for b in db.execute(select(CarrierBid.order_id).where(
            CarrierBid.order_id.in_(oids),
            CarrierBid.quote_amount.isnot(None))).all():
        counts[b[0]] = counts.get(b[0], 0) + 1

    out: List[Dict[str, Any]] = []
    for b, o in rows:
        t = latest.get(o.id)
        neg = negotiation_of(t) if t else {}
        i_am_target = bool(t and int(neg.get("target_org_id") or 0) == int(org))
        item: Dict[str, Any] = {
            "order_id": o.id, "order_no": o.order_no,
            "origin": o.origin, "dest": o.dest,
            "order_status": o.status, "order_status_label": STATUS_LABEL.get(o.status, o.status),
            "quote": float(b.quote_amount), "eta_h": float(b.quote_eta_h or 0),
            "round": int(b.bid_round or 1), "decision": b.decision,
            "at": b.created_at.isoformat(timespec="seconds") if b.created_at else None,
            "bid_count": counts.get(o.id, 0),
            "tender_status": t.status if t else None,
            "tender_status_label": (TENDER_STATUS_LABEL.get(t.status, t.status) if t else None),
            "deadline_at": (t.deadline_at.isoformat(timespec="seconds")
                            if t and t.deadline_at else None),
            "won": bool(t and t.winner_bid_id == b.id),
            "awaiting_me": bool(t and t.status == "negotiating" and i_am_target),
        }
        # 只有被还价的那家看得到协商轨迹：别家看到"有人被还价了"就是
        # 竞争对手的谈判筹码，公开它等于泄露定价空间。
        if i_am_target:
            item["negotiation"] = {
                "round": int(neg.get("round") or 1),
                "max_rounds": int(neg.get("max_rounds") or 0),
                "rounds_used": int(neg.get("rounds_used") or 0),
                "outcome": neg.get("outcome"),
                "turns": neg.get("turns") or [],
            }
        if t and t.status in ("awarded", "failed", "cancelled"):
            item["result"] = ("你中标了" if item["won"]
                              else "本单已定标" if t.status == "awarded" else "本单流标")
        out.append(item)
    return out


def quote(db: Session, user: Dict[str, Any], order_id: int) -> Dict[str, Any]:
    """试算并进入「候选」状态。不改承运商、不锁单，只是表达意向。"""
    org_id = user.get("org_id")
    prof = profile_of(db, org_id)
    if not prof:
        raise ValueError("当前账号没有承运商档案，请联系平台补录（每公里成本）")

    o, pub, plan = _open_triplet(db, order_id)
    prof = profile_of(db, org_id)
    if prof.cost_per_km is None:
        raise ValueError("承运商档案缺少「每公里成本」，无法试算收益")

    distance = plan.distance_km or Decimal("0")
    # 空驶：承运商当前位置 -> 该单起运地。识别不到位置时按 0 计并如实标注，
    # 让承运商自己知道"这个收益没算空车钱"。
    dh = routing.deadhead(prof.current_position, o.origin)
    p = pricing.carrier_profit(pub.publish_price, distance, prof.cost_per_km,
                               pub.carrier_fee_rate, dh.get("deadhead_km") or 0)

    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == org_id)).scalars().first()
    if bid is None:
        bid = CarrierBid(order_id=order_id, publish_id=pub.id, carrier_org_id=org_id,
                         carrier_user_id=user["user_id"])
        db.add(bid)
    if bid.decision == "accepted":
        raise ValueError("你已经接了这单")
    # 2026-10-18（S2）修掉旧副作用：这里原本会把订单从 published 翻成 candidate，
    # 于是"点了一次算收益、根本没打算接"也会改变订单状态。试算＝只想看看，
    # 不该产生业务后果；真正的意向表达走 POST /orders/{id}/bid（写 quote_amount）。
    bid.decision = bid.decision if bid.decision in ("bid", "accepted") else "candidate"
    bid.quoted_price = p["quoted"]
    bid.est_cost = p["est_cost"]
    bid.est_fee = p["carrier_fee"]
    bid.est_profit = p["profit"]
    bid.deadhead_km = p["deadhead_km"]
    bid.deadhead_cost = p["deadhead_cost"]
    bid.linehaul_cost = p["linehaul_cost"]
    bid.deadhead_from = (prof.current_position or "")[:255] or None

    # 试算不再改订单状态（S2）。candidate 只由投标或裁决推进。
    db.commit()
    return {"order_id": order_id, "order_no": o.order_no, "decision": bid.decision,
            "distance_km": float(distance),
            "cost_per_km": float(prof.cost_per_km),
            "quoted_price": float(p["quoted"]),
            "carrier_fee": float(p["carrier_fee"]),
            "linehaul_cost": float(p["linehaul_cost"]),
            "deadhead_km": float(p["deadhead_km"]),
            "deadhead_cost": float(p["deadhead_cost"]),
            "deadhead_from": bid.deadhead_from,
            "deadhead_note": "" if dh.get("ok") else dh.get("reason", ""),
            "est_cost": float(p["est_cost"]),
            "profit": float(p["profit"]),
            "formula": "收益 = 运费 − 承运商侧服务费 − 干线成本 − 空驶成本",
            "order_status": o.status}


def accept_core(db: Session, user: Dict[str, Any], order_id: int,
                position_text: str = "") -> Dict[str, Any]:
    """接单的**执行体**：行锁 + 状态校验做互斥；同一单只能一个人拿下。

    S5 起这个函数只由 ``carrier_daemon`` 的 ``invoke:accept_order`` 调用。
    HTTP 路由与对话 agent 都只投消息，不再直接调它——否则"关掉承运商 agent
    也能接单"，第 5 条检验就是假的。

    名字从 ``accept`` 改成 ``accept_core`` 是刻意的：改不动名字的代码，
    调用点就没人会去检查。
    """
    org_id = user.get("org_id")
    # 锁订单行：两个承运商同时点接单时，只有一个能穿过状态校验
    o = db.execute(select(ShipperOrder).where(
        ShipperOrder.id == order_id).with_for_update()).scalars().first()
    if not o:
        raise ValueError("订单不存在")
    if o.status == "claimed" or o.status == "in_transit":
        raise ValueError("这单已经被接走了")
    if o.status not in ("published", "candidate"):
        raise ValueError(f"订单当前状态「{STATUS_LABEL.get(o.status, o.status)}」不能接单")

    pub = db.execute(select(OrderPublish).where(
        OrderPublish.order_id == order_id,
        OrderPublish.status == "open").order_by(OrderPublish.id.desc())).scalars().first()
    if not pub:
        raise ValueError("这单已下架")

    # 已定标给别家时，非中标方不能靠"点接单"截胡。这条校验只能放在这里：
    # 裁决只是把订单推进到 candidate，接单才是真正占用的那个动作。
    from . import tender_ops

    t = tender_ops.latest_tender(db, order_id)
    if t and t.status == "awarded" and t.winner_bid_id:
        win = db.get(CarrierBid, t.winner_bid_id)
        if win and win.carrier_org_id != org_id:
            raise ValueError("这单已由招标裁决给其他承运商，不能接单")

    plan = db.get(OrderPlan, pub.plan_id)
    prof = profile_of(db, org_id)
    if not prof:
        raise ValueError("当前账号没有承运商档案")

    # 接单时以"本次自报的位置"为准算空驶（它比档案里的位置更新）：
    # 承运商说"我现在在保定"，那空驶就是从保定到起运地，不是从档案旧位置算。
    pos = (position_text or "").strip() or (prof.current_position or "")
    dh = routing.deadhead(pos, o.origin)
    p = pricing.carrier_profit(pub.publish_price, plan.distance_km or Decimal("0"),
                               prof.cost_per_km, pub.carrier_fee_rate,
                               dh.get("deadhead_km") or 0)

    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == org_id)).scalars().first()
    if bid is None:
        bid = CarrierBid(order_id=order_id, publish_id=pub.id, carrier_org_id=org_id)
        db.add(bid)
    bid.carrier_user_id = user["user_id"]
    bid.decision = "accepted"
    bid.quoted_price = p["quoted"]
    bid.est_cost = p["est_cost"]
    bid.est_fee = p["carrier_fee"]
    bid.est_profit = p["profit"]
    bid.deadhead_km = p["deadhead_km"]
    bid.deadhead_cost = p["deadhead_cost"]
    bid.linehaul_cost = p["linehaul_cost"]
    bid.deadhead_from = pos[:255] or None
    bid.position_text = pos[:255] or None
    bid.decided_at = datetime.now()

    # 其余候选一律置为放弃，避免池子里残留"看起来还能接"的意向
    for other in db.execute(select(CarrierBid).where(
            CarrierBid.order_id == order_id,
            CarrierBid.carrier_org_id != org_id,
            CarrierBid.decision.in_(("candidate", "bid")))).scalars().all():
        other.decision = "withdrawn"

    o.carrier_org_id = org_id
    o.carrier_user_id = user["user_id"]
    pub.status = "closed"
    pub.closed_at = datetime.now()

    if o.status == "published":
        transition(db, o, "candidate", "carrier_candidate", "carrier", user["user_id"],
                   {"carrier_org_id": org_id})
    transition(db, o, "claimed", "order_claimed", "carrier", user["user_id"],
               {"carrier_org_id": org_id, "position_text": bid.position_text,
                "quoted_price": float(p["quoted"]), "est_profit": float(p["profit"])})
    db.commit()
    return {"order_id": order_id, "order_no": o.order_no, "status": o.status,
            "position_text": bid.position_text, "profit": float(p["profit"])}


def reject(db: Session, user: Dict[str, Any], order_id: int) -> Dict[str, Any]:
    org_id = user.get("org_id")
    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == org_id)).scalars().first()
    if not bid:
        raise ValueError("没有这单的记录")
    if bid.decision == "accepted":
        raise ValueError("已接单的订单不能放弃，请走异常上报流程")
    bid.decision = "rejected"
    bid.decided_at = datetime.now()

    # 若该单没有其他候选人，订单退回挂牌状态
    o = db.get(ShipperOrder, order_id)
    alive = db.execute(select(CarrierBid.id).where(
        CarrierBid.order_id == order_id,
        CarrierBid.decision == "candidate")).first()
    if o and o.status == "candidate" and not alive:
        transition(db, o, "published", "carrier_withdrew", "carrier", user["user_id"], {})
    db.commit()
    return {"order_id": order_id, "decision": bid.decision}


def report_exception(db: Session, user: Dict[str, Any], order_id: int,
                     position_text: str, description: str) -> Dict[str, Any]:
    """承运商上报异常。位置为文本，由承运商自报（不再依赖车载 IoT）。"""
    o = db.get(ShipperOrder, order_id)
    if not o:
        raise ValueError("订单不存在")
    if o.carrier_org_id != user.get("org_id"):
        raise ValueError("这不是你的运输订单")
    if o.status not in ("claimed", "in_transit"):
        raise ValueError(f"订单当前状态「{STATUS_LABEL.get(o.status, o.status)}」不能上报异常")

    e = ExceptionReport(order_id=order_id, reporter_user_id=user["user_id"],
                        reporter_org_id=user.get("org_id"),
                        position_text=(position_text or "").strip()[:255],
                        description=(description or "").strip()[:2000],
                        status="open")
    db.add(e)
    db.flush()
    transition(db, o, "exception", "exception_reported", "carrier", user["user_id"],
               {"exception_id": e.id, "position_text": e.position_text,
                "description": e.description})
    db.commit()
    return {"exception_id": e.id, "order_id": order_id, "order_no": o.order_no,
            "order_status": o.status, "position_text": e.position_text}


def my_orders(db: Session, user: Dict[str, Any], limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.execute(select(ShipperOrder).where(
        ShipperOrder.carrier_org_id == user.get("org_id")
    ).order_by(ShipperOrder.id.desc()).limit(limit)).scalars().all()
    out = []
    for o in rows:
        bid = db.execute(select(CarrierBid).where(
            CarrierBid.order_id == o.id,
            CarrierBid.carrier_org_id == user.get("org_id"))).scalars().first()
        out.append({"order_id": o.id, "order_no": o.order_no,
                    "origin": o.origin, "dest": o.dest,
                    "status": o.status, "status_label": STATUS_LABEL.get(o.status, o.status),
                    "est_profit": float(bid.est_profit) if bid and bid.est_profit is not None else None,
                    "position_text": bid.position_text if bid else None,
                    "decided_at": bid.decided_at.isoformat(timespec="seconds")
                    if bid and bid.decided_at else None})
    return out


def _open_triplet(db: Session, order_id: int):
    o = db.get(ShipperOrder, order_id)
    if not o:
        raise ValueError("订单不存在")
    pub = db.execute(select(OrderPublish).where(
        OrderPublish.order_id == order_id,
        OrderPublish.status == "open").order_by(OrderPublish.id.desc())).scalars().first()
    if not pub:
        raise ValueError("这单不在挂牌中")
    plan = db.get(OrderPlan, pub.plan_id)
    if not plan:
        raise ValueError("这单缺少方案")
    return o, pub, plan
