"""招标 / 投标 / 裁决的数据库层（S2）。

`services/tender.py` 是纯规则（报价公式、硬过滤、打分、裁决），本文件负责
**把数据取对、把结果落对**。两者分开的理由：规则要能用假数据把边界测穿，
接了 Session 之后"零投标"这种场景就得先造一张订单才能测。

三条口径必须先说清，否则金额会算重：

1. ``Tender.budget`` 是**该单可给的运费上限**，等于 ``order_publish.publish_price``，
   不是货主的 ``budget_max``。承运商看不到预算，也不该拿预算当自己的基准；
   平台能付出去的就是扣完货主侧服务费后的那个数。

2. ``carrier_bid.quote_amount`` 是承运商要求的**运费**（和 publish_price 同一空间）。
   它与 ``quoted_price`` 的区别是：后者是服务端按挂牌价回填的"你能拿多少"，
   从来没经过承运商。

3. 成本换运费要除以 ``(1 − 承运商侧费率)``：承运商实收 = 报价 × (1 − 费率)，
   所以"成本刚好覆盖"的报价 = 成本 ÷ (1 − 费率)。少这一步，报出来的价
   看着有 8% 利润，实际扣完服务费是亏的。

**不改金额链**：裁决只决定"谁中标"，不改 ``publish_price``。
改它会破坏 ``gross = 货主侧服务费 + 承运商侧服务费 + 承运商实收`` 这条守恒关系
（挂牌价和预算是两个独立变量了），那是结算口径的改造，不在本期。
所以中标价低于挂牌价时，承运商仍按挂牌价结算——对它不劣，这一点在文档里写明。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (CarrierBid, CarrierProfile, OrderPlan, OrderPublish,
                      ShipperOrder, Tender)
from . import audit, pricing, routing, runtime
from . import tender as rules
from .orders import add_event, transition

# 收标时长。太短没人来得及报，太长订单压着不动。
TENDER_MINUTES = 30
# 允许投标的订单状态。candidate 也允许：已经有人表达意向的单不该锁死别人投标。
BIDDABLE = ("published", "candidate")

TENDER_STATUS_LABEL = {
    "open": "收标中",
    "negotiating": "协商中",
    "awarded": "已定标",
    "failed": "流标",
    "cancelled": "已取消",
}


class TenderError(ValueError):
    """业务性拒绝。路由层转 409，Agent 直接读 message。"""


def _now() -> datetime:
    return datetime.now()


def _q(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(pricing.CENT, rounding=ROUND_HALF_UP)


def _num(v: Any) -> float:
    return float(Decimal(str(v)))


def _jsonable(v: Any) -> Any:
    """Decimal / datetime 不能直接进 JSON 列。"""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _as_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


def weights_of(db: Session) -> Dict[str, float]:
    """裁决权重。库里没有或写坏了就回落到规则默认值，不让裁决停摆。"""
    conf = _as_dict(runtime.get(db, "tender.weights"))
    w = {k: float(conf.get(k, v)) for k, v in rules.WEIGHTS_DEFAULT.items()}
    return w if sum(w.values()) > 0 else dict(rules.WEIGHTS_DEFAULT)


def max_rounds_of(db: Session) -> int:
    try:
        return max(1, int(str(runtime.get(db, "tender.max_rounds") or rules.MAX_ROUNDS)))
    except (TypeError, ValueError):
        return rules.MAX_ROUNDS


def utility_of(db: Session, agent_key: str) -> Dict[str, float]:
    """读某个 agent 的目标权重（``agent_actor.utility``）。

    这是"多 Agent"里唯一把立场写成数据的地方，必须真的被决策读到并且用上。
    只存不读的话，"四个 agent 目标不同"就只是一句没有代码支撑的说法——
    改造前 ``order_plan.arbitration`` 一直空着，正是同一类问题。
    """
    from ..models import AgentActor

    try:
        row = db.execute(select(AgentActor).where(
            AgentActor.agent_key == agent_key)).scalars().first()
        return dict(row.utility or {}) if row else {}
    except Exception:                       # noqa: BLE001 表不可用时用默认口径
        return {}


# ---------------------------------------------------------------------------
# 金额换算
# ---------------------------------------------------------------------------
def freight_floor(cost_total: Any, carrier_rate: Any) -> Decimal:
    """成本底线换成**运费**口径：承运商实收 = 报价 × (1 − 费率)。"""
    cost = Decimal(str(cost_total or 0))
    cr = Decimal(str(carrier_rate or 0))
    denom = Decimal("1") - cr
    if denom <= 0:                       # 费率被配成 100%，没意义，原样返回
        return _q(cost)
    return _q(cost / denom)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def active_tender(db: Session, order_id: int) -> Optional[Tender]:
    return db.execute(select(Tender).where(
        Tender.order_id == int(order_id),
        Tender.status.in_(("open", "negotiating"))).order_by(
        Tender.id.desc())).scalars().first()


def latest_tender(db: Session, order_id: int) -> Optional[Tender]:
    return db.execute(select(Tender).where(
        Tender.order_id == int(order_id)).order_by(Tender.id.desc())).scalars().first()


def _open_publish(db: Session, order_id: int) -> Optional[OrderPublish]:
    return db.execute(select(OrderPublish).where(
        OrderPublish.order_id == int(order_id),
        OrderPublish.status == "open").order_by(OrderPublish.id.desc())).scalars().first()


def _profile(db: Session, org_id: Optional[int]) -> Optional[CarrierProfile]:
    if not org_id:
        return None
    return db.execute(select(CarrierProfile).where(
        CarrierProfile.org_id == int(org_id))).scalars().first()


def _org_names(db: Session, org_ids: Any) -> Dict[int, str]:
    """机构名。看板要回答"是哪家在报"，只给 id 等于让调度自己拿 id 去别处对表。"""
    from ..models import SysOrg

    ids = sorted({int(x) for x in (org_ids or []) if x})
    if not ids:
        return {}
    return {o.id: o.name for o in db.execute(
        select(SysOrg).where(SysOrg.id.in_(ids))).scalars().all()}


def _bid_stats(db: Session, order_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """各单"收到过报价"的家数、价格区间、最新报价时间。一次查完，避免看板 N+1。

    **不能只数 decision in (bid, candidate)**：定标时落败方会被置 ``rejected``
    （这是对的，界面上要能看出谁没中），于是刚裁决完"报价 2 家"会突然变成 1 家——
    看板看起来像丢了数据。这里数的是"报过价的家数"，与最终决策无关；
    主动退出（``withdrawn``）不算在竞争里。
    """
    stats: Dict[int, Dict[str, Any]] = {}
    ids = [int(x) for x in order_ids if x]
    if not ids:
        return stats
    rows = db.execute(select(CarrierBid).where(
        CarrierBid.order_id.in_(ids)).order_by(CarrierBid.id)).scalars().all()
    for b in rows:
        if b.quote_amount is None or b.decision == "withdrawn":
            continue
        s = stats.setdefault(b.order_id, {"count": 0, "min": None, "max": None,
                                          "last_at": None})
        q = float(b.quote_amount)
        s["count"] += 1
        s["min"] = q if s["min"] is None else min(s["min"], q)
        s["max"] = q if s["max"] is None else max(s["max"], q)
        if b.created_at:
            ts = b.created_at.isoformat(timespec="seconds")
            s["last_at"] = ts if not s["last_at"] else max(s["last_at"], ts)
    return stats


def board(db: Session, *, status: str = "active", limit: int = 50) -> Dict[str, Any]:
    """竞价看板：一屏看清"哪些单在竞、几家在报、报到什么价、什么时候截止"。

    不复用 ``view`` 的理由有两条：``view`` 是单票视角（带几十 KB 的裁决证据），
    看板要的是跨单的一行摘要；逐单调 ``view`` 还会变成 N+1。
    """
    q = select(Tender)
    if status and status != "all":
        wanted = ("open", "negotiating") if status == "active" else (status,)
        q = q.where(Tender.status.in_(wanted))
    order = (Tender.deadline_at.asc() if status in ("active", "open")
             else Tender.id.desc())
    tenders = db.execute(q.order_by(order).limit(max(1, min(int(limit), 200)))).scalars().all()
    if not tenders:
        return {"items": [], "counts": {}, "filter": status}

    order_ids = sorted({int(t.order_id) for t in tenders})
    ords = {o.id: o for o in db.execute(select(ShipperOrder).where(
        ShipperOrder.id.in_(order_ids))).scalars().all()}
    stats = _bid_stats(db, order_ids)
    negs = {t.id: negotiation_of(t) for t in tenders}
    # 还价对象有两个来源，别只认一个：已经开谈的在 negotiation 里，
    # 刚裁决成"协商中"但还没发第一轮出价的只在 arbitration.target 里。
    # 只看前者的话，看板会显示"协商中 · 在跟谁谈：—"。
    targets = {t.id: (negs[t.id].get("target_org_id")
                      or ((t.arbitration or {}).get("target") or {}).get("carrier_org_id"))
               for t in tenders}
    names = _org_names(db, list(targets.values()))

    now = _now()
    items: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for t in tenders:
        o = ords.get(int(t.order_id))
        if o is None:
            continue
        s = stats.get(o.id, {"count": 0, "min": None, "max": None, "last_at": None})
        neg = negs.get(t.id) or {}
        budget = _num(t.budget)
        left = int((t.deadline_at - now).total_seconds()) if t.deadline_at else None
        counts[t.status] = counts.get(t.status, 0) + 1
        items.append({
            "tender_id": t.id, "order_id": o.id, "order_no": o.order_no,
            "origin": o.origin, "dest": o.dest, "cargo_type": o.cargo_type,
            "weight_t": _num(o.weight_t) if o.weight_t is not None else None,
            "order_status": o.status,
            "status": t.status,
            "status_label": TENDER_STATUS_LABEL.get(t.status, t.status),
            "round_no": int(t.round_no or 1),
            "budget": budget,
            # 协商成交时"最后成交多少"和"中标家当初报了多少"是两个数，
            # 看板的中标金额要给成交价，否则会显示一个超过可付上限的数字
            "final_amount": (_num(neg.get("final_amount"))
                             if neg.get("final_amount") is not None else None),
            "deadline_at": t.deadline_at.isoformat(timespec="seconds") if t.deadline_at else None,
            # 负数表示已过截止时间还在收标：看板要能一眼看出"该裁决了"
            "seconds_left": left,
            "bid_count": int(s["count"]),
            "min_quote": s["min"], "max_quote": s["max"],
            "last_bid_at": s["last_at"],
            "over_budget": bool(s["min"] is not None and budget is not None
                                and s["min"] > budget),
            "winner": ({"bid_id": t.winner_bid_id, "carrier_org_id": None,
                        "carrier_org": None, "amount": None}
                       if t.winner_bid_id else None),
            "target_org_id": targets.get(t.id),
            "target_org": names.get(targets.get(t.id)) if targets.get(t.id) else None,
            "negotiation": ({"round": int(neg.get("round") or 1),
                             "max_rounds": int(neg.get("max_rounds") or 0),
                             "rounds_used": int(neg.get("rounds_used") or 0),
                             "outcome": neg.get("outcome"),
                             "gap": neg.get("gap")}
                            if t.status == "negotiating" or neg.get("outcome") else None),
        })

    # 中标机构名一次补齐（上面先在循环里占位，避免每单多一次查询）
    win_ids = [t.winner_bid_id for t in tenders if t.winner_bid_id]
    if win_ids:
        bids = {b.id: b for b in db.execute(select(CarrierBid).where(
            CarrierBid.id.in_([int(x) for x in win_ids]))).scalars().all()}
        wnames = _org_names(db, [b.carrier_org_id for b in bids.values()])
        for it in items:
            wid = (it.get("winner") or {}).get("bid_id")
            if wid and int(wid) in bids:
                b = bids[int(wid)]
                it["winner"].update({"carrier_org_id": b.carrier_org_id,
                                     "carrier_org": wnames.get(b.carrier_org_id),
                                     "amount": (it.get("final_amount")
                                                if it.get("final_amount") is not None
                                                else _num(b.quote_amount))})
    return {"items": items, "counts": counts, "filter": status}


def required_eta_h(order: ShipperOrder) -> Optional[Decimal]:
    """从承诺时限反推"还剩几小时"。没填绝对时间就不设时限约束，不猜。"""
    if not getattr(order, "required_by_at", None):
        return None
    left = (order.required_by_at - _now()).total_seconds() / 3600.0
    return Decimal(str(round(left, 2)))


# ---------------------------------------------------------------------------
# 开标
# ---------------------------------------------------------------------------
def open_tender(db: Session, order: ShipperOrder, user: Dict[str, Any], *,
                minutes: Optional[int] = None, commit: bool = True) -> Dict[str, Any]:
    """挂牌即开标。同一张挂牌单只允许一个进行中的招标单（幂等）。"""
    if order.status not in ("published", "candidate"):
        raise TenderError(
            f"订单当前状态「{order.status}」不能开标，请先挂牌")

    pub = _open_publish(db, order.id)
    if not pub:
        raise TenderError("这单不在挂牌中（没有 open 状态的挂牌单）")

    exist = active_tender(db, order.id)
    if exist and exist.publish_id == pub.id:
        return _tender_brief(exist, reused=True)

    # 换了挂牌价重新挂牌：旧招标单作废，不能让两份标同时存在
    for old in db.execute(select(Tender).where(
            Tender.order_id == order.id,
            Tender.status.in_(("open", "negotiating")))).scalars().all():
        old.status = "cancelled"
        old.closed_at = _now()

    mins = int(minutes if minutes is not None else TENDER_MINUTES)
    t = Tender(order_id=order.id, publish_id=pub.id, plan_id=pub.plan_id,
               budget=_q(pub.publish_price), round_no=1,
               deadline_at=_now() + timedelta(minutes=mins),
               status="open", created_by="dispatch", arbitration=None)
    db.add(t)
    db.flush()
    add_event(db, order.id, "tender_opened", "dispatcher", user.get("user_id"),
              {"tender_id": t.id, "budget": float(t.budget),
               "deadline_at": t.deadline_at.isoformat(timespec="seconds"),
               "minutes": mins})
    audit.log(db, user, "tender.open", target_type="order", target_id=order.id,
              detail={"tender_id": t.id, "budget": float(t.budget), "minutes": mins},
              agent_key="dispatch")
    if commit:
        db.commit()
    else:
        db.flush()
    return _tender_brief(t, reused=False)


def _tender_brief(t: Tender, *, reused: bool) -> Dict[str, Any]:
    return {"tender_id": t.id, "order_id": t.order_id, "publish_id": t.publish_id,
            "status": t.status, "status_label": TENDER_STATUS_LABEL.get(t.status, t.status),
            "budget": _num(t.budget), "round_no": int(t.round_no or 1),
            "deadline_at": t.deadline_at.isoformat(timespec="seconds") if t.deadline_at else None,
            "reused": reused}


# ---------------------------------------------------------------------------
# 投标
# ---------------------------------------------------------------------------
def submit_bid(db: Session, user: Dict[str, Any], order_id: int, *,
               quote_amount: Optional[Any] = None, eta_h: Optional[Any] = None,
               note: str = "", position_text: str = "",
               commit: bool = True) -> Dict[str, Any]:
    """承运商投标。**不改变订单状态**——这是本次要修掉的旧副作用。

    旧实现里承运商点一次"算收益"，订单就从 ``published`` 翻成 ``candidate``，
    即使它根本没打算接单。那等于让"看了一眼"变成"我要接"，把订单状态当成了
    浏览记录。投标只写 ``carrier_bid`` 一行，订单状态由裁决推进。
    """
    org_id = user.get("org_id")
    if not org_id:
        raise TenderError("当前账号没有承运商归口组织")

    o = db.get(ShipperOrder, int(order_id))
    if not o:
        raise TenderError("订单不存在")
    if o.status in ("claimed", "in_transit"):
        raise TenderError("这单已经被接走了")
    if o.status not in BIDDABLE:
        raise TenderError(f"订单当前状态「{o.status}」不能投标")

    pub = _open_publish(db, o.id)
    if not pub:
        raise TenderError("这单不在挂牌中")

    prof = _profile(db, org_id)
    if not prof or prof.cost_per_km is None:
        raise TenderError("承运商档案缺少「每公里成本」，无法报价")

    t = active_tender(db, o.id)
    if t is None:
        # 历史挂牌单没有招标单（v6 之前挂的）：惰性补开，避免老订单无法投标。
        open_tender(db, o, user, commit=False)
        t = active_tender(db, o.id)
        if t is None:
            raise TenderError("开标失败，请联系调度")
    if t.status == "negotiating":
        # 协商期间重新投标会绕过谈判轨迹：想降价就该走 counter 回应，
        # 否则 `tender.negotiation` 里记的价和实际报价会对不上。
        raise TenderError("招标已进入协商阶段，不能再投放新报价，请等待调度还价后回应")
    if t.deadline_at and _now() > t.deadline_at:
        raise TenderError(
            f"收标已于 {t.deadline_at:%m-%d %H:%M} 截止，请联系调度延期")

    plan = db.get(OrderPlan, pub.plan_id)
    if not plan or not plan.distance_km:
        raise TenderError("这单缺少有效方案（里程），无法报价")

    rate = Decimal(str(prof.cost_per_km))
    pos = (position_text or "").strip() or (prof.current_position or "")
    dh = routing.deadhead(pos, o.origin)
    dh_km = Decimal(str(dh.get("deadhead_km") or 0))
    linehaul = _q(Decimal(str(plan.distance_km)) * rate)
    deadhead_cost = _q(dh_km * rate)
    cost_total = _q(linehaul + deadhead_cost)

    _, cr = runtime.active_fee(db)
    floor = freight_floor(cost_total, cr)

    # 资质：False 明确挡掉；None（未登记）放行但标注待核验
    qual_pending: List[str] = []
    if o.temp_ctrl:
        if prof.temp_ctrl is False:
            raise TenderError("你的档案没有温控资质，不能承运冷链货")
        if prof.temp_ctrl is None:
            qual_pending.append("温控资质待核验")
    if o.hazmat:
        if prof.hazmat is False:
            raise TenderError("你的档案没有危品资质，不能承运危化品")
        if prof.hazmat is None:
            qual_pending.append("危品资质待核验")

    auto = False
    profit_rate = rules.profit_rate_from(utility_of(db, f"carrier:{org_id}"))
    if quote_amount in (None, ""):
        calc = rules.carrier_quote(floor, budget=pub.publish_price,
                                   target_profit_rate=profit_rate)
        if calc["below_cost"]:
            raise TenderError(
                f"成本底线 ¥{floor} 已高于该单可付运费 ¥{_q(pub.publish_price)}，"
                "接单必亏，建议弃标")
        quote = calc["quote"]
        auto = True
    else:
        quote = _q(quote_amount)
    if quote <= 0:
        raise TenderError("报价必须大于 0")

    commit_eta = _q(eta_h) if eta_h not in (None, "") else _q(plan.eta_h or 0)
    if commit_eta <= 0:
        raise TenderError("缺少承诺时效（小时）")

    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == o.id,
        CarrierBid.carrier_org_id == org_id)).scalars().first()
    if bid is None:
        bid = CarrierBid(order_id=o.id, publish_id=pub.id, carrier_org_id=org_id,
                         carrier_user_id=user.get("user_id"), bid_round=1,
                         bid_history=[])
        db.add(bid)
        db.flush()
    elif bid.decision == "accepted":
        raise TenderError("你已经接了这单，不能再改报价")

    round_no = int(bid.bid_round or 0) + 1 if bid.quote_amount is not None else 1
    history = list(bid.bid_history or [])
    history.append({"round": round_no, "amount": _num(quote), "eta_h": _num(commit_eta),
                    "at": _now().isoformat(timespec="seconds"),
                    "auto": auto, "floor": _num(floor)})

    bid.publish_id = pub.id
    bid.carrier_user_id = user.get("user_id")
    bid.decision = "bid"
    bid.quote_amount = quote
    bid.quote_eta_h = commit_eta
    bid.bid_note = (note or ("；".join(qual_pending) if qual_pending else None) or None)
    if bid.bid_note and qual_pending:
        bid.bid_note = (str(bid.bid_note)[:180] + "（" + "、".join(qual_pending) + "）")[:200]
    bid.bid_round = round_no
    bid.bid_history = history
    # quoted_price 保留原义（服务端按挂牌价回填），别和历史数据混淆
    bid.quoted_price = _q(pub.publish_price)
    p = pricing.carrier_profit(quote, plan.distance_km, rate, cr, dh_km)
    bid.est_cost = cost_total
    bid.est_fee = p["carrier_fee"]
    bid.est_profit = p["profit"]
    bid.linehaul_cost = linehaul
    bid.deadhead_km = _q(dh_km)
    bid.deadhead_cost = deadhead_cost
    bid.deadhead_from = (pos or "")[:255] or None
    bid.position_text = (pos or "")[:255] or None
    bid.decided_at = _now()

    add_event(db, o.id, "tender_bid", "carrier", user.get("user_id"),
              {"tender_id": t.id, "carrier_org_id": org_id, "quote": _num(quote),
               "eta_h": _num(commit_eta), "round": round_no,
               "est_profit": _num(p["profit"]),
               "deadhead_note": "" if dh.get("ok") else dh.get("reason", "")})
    audit.log(db, user, "tender.bid", target_type="order", target_id=o.id,
              detail={"tender_id": t.id, "quote": _num(quote),
                      "floor": _num(floor), "auto": auto,
                      "profit_rate": float(profit_rate)},
              agent_key=f"carrier:{org_id}")
    if commit:
        db.commit()
    else:
        db.flush()

    return {"tender_id": t.id, "order_id": o.id, "order_no": o.order_no,
            "bid_id": bid.id, "quote": _num(quote), "eta_h": _num(commit_eta),
            "auto_priced": auto, "floor": _num(floor),
            "cost_total": _num(cost_total), "linehaul_cost": _num(linehaul),
            "deadhead_km": _num(dh_km), "deadhead_cost": _num(deadhead_cost),
            "carrier_fee": _num(p["carrier_fee"]), "profit": _num(p["profit"]),
            "profit_rate": (float(p["profit"] / cost_total) if cost_total > 0 else None),
            "bid_round": round_no, "qual_pending": qual_pending,
            "deadhead_note": "" if dh.get("ok") else dh.get("reason", ""),
            # 订单状态必须原样返回：前端据此显示"投标不改变订单状态"
            "order_status": o.status}


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------
def _bid_rows(db: Session, order_id: int) -> List[Dict[str, Any]]:
    """把 carrier_bid 行摊成规则层要的结构（含承运商档案里的资质与评分）。"""
    out = []
    for b in db.execute(select(CarrierBid).where(
            CarrierBid.order_id == order_id,
            CarrierBid.decision.in_(("bid", "candidate")))).scalars().all():
        prof = _profile(db, b.carrier_org_id)
        quote = b.quote_amount if b.quote_amount is not None else b.quoted_price
        if quote is None:
            continue
        out.append({
            "bid_id": b.id, "carrier_org_id": b.carrier_org_id,
            "quote": Decimal(str(quote)),
            "eta_h": Decimal(str(b.quote_eta_h or 0)),
            "deadhead_km": Decimal(str(b.deadhead_km or 0)),
            "rating": Decimal(str(getattr(prof, "rating", None) or 5)),
            "capacity_t": Decimal(str(getattr(prof, "capacity_t", None) or 0)),
            "temp_ctrl": getattr(prof, "temp_ctrl", None),
            "hazmat": getattr(prof, "hazmat", None),
            "est_profit": (Decimal(str(b.est_profit)) if b.est_profit is not None else None),
        })
    return out


def _request(db: Session, order: ShipperOrder, t: Tender) -> Dict[str, Any]:
    # 资质未登记（None）时按"满足"交给规则层，由资质校验在投标时已挡掉 False，
    # 这里传 True 而不是 None，规则层不处理三态。
    prof_flags = {}
    for b in _bid_rows(db, order.id):
        prof_flags[b["carrier_org_id"]] = (b["temp_ctrl"] is not False,
                                           b["hazmat"] is not False)
    return {"budget": Decimal(str(t.budget)),
            "required_eta_h": required_eta_h(order),
            "weight_t": Decimal(str(order.weight_t or 0)),
            "need_temp": bool(order.temp_ctrl), "need_haz": bool(order.hazmat),
            "deadhead_max_km": rules.DEADHEAD_MAX_KM,
            "_qual": prof_flags}


def _apply_award(db: Session, order: ShipperOrder, t: Tender, *,
                 bid_id: Optional[int], carrier_org_id: Optional[int],
                 quote: Any, score: Any, user: Dict[str, Any],
                 note: str = "") -> None:
    """定标落库。收标裁决与协商成交共用一份——两条路都必须留下同样的证据。"""
    t.status = "awarded"
    t.winner_bid_id = int(bid_id) if bid_id else None
    t.closed_at = _now()
    # 落败的投标显式置 rejected：否则它们停在 bid，界面上看不出谁没中
    for b in db.execute(select(CarrierBid).where(
            CarrierBid.order_id == order.id,
            CarrierBid.decision.in_(("bid", "candidate")))).scalars().all():
        if b.id != t.winner_bid_id:
            b.decision = "rejected"
            b.decided_at = _now()
    payload = {"tender_id": t.id, "bid_id": t.winner_bid_id,
               "carrier_org_id": carrier_org_id,
               "quote": float(quote) if quote is not None else None,
               "score": score}
    if note:
        payload["note"] = note
    if order.status == "published":
        transition(db, order, "candidate", "tender_awarded", "dispatcher",
                   user.get("user_id"), payload)
    else:
        payload.setdefault("note", "订单状态未变（非 published）")
        add_event(db, order.id, "tender_awarded", "dispatcher", user.get("user_id"),
                  payload)


def close_tender(db: Session, order: ShipperOrder, user: Dict[str, Any], *,
                 force: bool = False, commit: bool = True) -> Dict[str, Any]:
    """收标截止后裁决。幂等：已裁决过的直接返回存好的证据，不重算。

    ``force=True`` 供人工提前截止；不加就只在截止时间到点后裁决。
    """
    t = active_tender(db, order.id)
    if t is None:
        t = latest_tender(db, order.id)
        if t is None:
            raise TenderError("这单还没有招标单")
        return {"tender_id": t.id, "status": t.status,
                "status_label": TENDER_STATUS_LABEL.get(t.status, t.status),
                "closed": t.status != "open", "reused": True,
                "arbitration": t.arbitration,
                "reason": "招标已结束，未重新裁决"}

    if not force and t.deadline_at and _now() < t.deadline_at:
        return {"tender_id": t.id, "status": t.status,
                "status_label": TENDER_STATUS_LABEL.get(t.status, t.status),
                "closed": False, "reused": False,
                "deadline_at": t.deadline_at.isoformat(timespec="seconds"),
                "reason": f"收标截止时间 {t.deadline_at:%H:%M} 未到"}

    rows = _bid_rows(db, order.id)
    req = _request(db, order, t)
    # 资质三态在这里收口成布尔：False 已在投标时挡掉，剩下的按可承运处理
    for row in rows:
        ok_t, ok_h = req["_qual"].get(row["carrier_org_id"], (True, True))
        row["temp_ctrl"] = ok_t
        row["hazmat"] = ok_h
    req.pop("_qual", None)

    w = weights_of(db)
    dispatch_utility = utility_of(db, "dispatch")
    ratio_cap = rules.negotiate_ratio_from(dispatch_utility)
    result = rules.decide(rows, req, weights=w,
                          extended=int(t.round_no or 1) > 1,
                          max_rounds=max_rounds_of(db),
                          negotiate_max_ratio=ratio_cap)
    evidence = _jsonable(result)
    evidence["decided_at"] = _now().isoformat(timespec="seconds")
    evidence["tender_id"] = t.id
    # 决策依据要能回溯：权重与可谈上限分别是"平台配置"和"调度自己的目标"，
    # 两者混在一起就说不清下次改了谁的参数会改变结果。
    evidence["policy"] = {"weights": _jsonable(w),
                          "dispatch_utility": _jsonable(dispatch_utility),
                          "negotiate_max_ratio": float(ratio_cap)}
    t.arbitration = evidence

    plan = db.get(OrderPlan, t.plan_id or order.active_plan_id or 0)
    if plan:
        # order_plan.arbitration 从建库起就是空的，这里第一次真正写上：
        # "为什么是这家"必须能查，否则裁决就只是"选了个看着顺眼的"。
        plan.arbitration = {**evidence, "order_id": order.id}

    status = result.get("status")
    out: Dict[str, Any] = {"tender_id": t.id, "closed": False, "reused": False,
                           "arbitration": evidence, "reason": result.get("reason")}

    if status == "awarded":
        winner = result["winner"]
        _apply_award(db, order, t, bid_id=winner.get("bid_id"),
                     carrier_org_id=winner.get("carrier_org_id"),
                     quote=winner.get("quote"), score=winner.get("score"), user=user)
        out.update({"closed": True, "status": t.status,
                    "status_label": TENDER_STATUS_LABEL[t.status],
                    "winner": winner, "winner_bid_id": t.winner_bid_id})
    elif status == "negotiating":
        t.status = "negotiating"
        add_event(db, order.id, "tender_negotiating", "dispatcher", user.get("user_id"),
                  {"tender_id": t.id, "target": result["target"].get("carrier_org_id"),
                   "over_ratio": float(result.get("over_ratio") or 0)})
        out.update({"closed": False, "status": t.status,
                    "status_label": TENDER_STATUS_LABEL[t.status],
                    "target": result.get("target")})
    elif status == "extended":
        t.round_no = int(t.round_no or 1) + 1
        t.deadline_at = _now() + timedelta(minutes=rules.EXTEND_MINUTES)
        add_event(db, order.id, "tender_extended", "dispatcher", user.get("user_id"),
                  {"tender_id": t.id, "round_no": t.round_no,
                   "deadline_at": t.deadline_at.isoformat(timespec="seconds")})
        out.update({"closed": False, "status": "open", "status_label": TENDER_STATUS_LABEL["open"],
                    "round_no": t.round_no,
                    "deadline_at": t.deadline_at.isoformat(timespec="seconds")})
    else:
        t.status = "failed"
        t.closed_at = _now()
        add_event(db, order.id, "tender_failed", "dispatcher", user.get("user_id"),
                  {"tender_id": t.id, "reason": result.get("reason")})
        out.update({"closed": True, "status": t.status,
                    "status_label": TENDER_STATUS_LABEL[t.status]})

    audit.log(db, user, "tender.close", target_type="order", target_id=order.id,
              reason=result.get("reason"), detail={"tender_id": t.id, "status": t.status,
                                                   "bids": len(rows)},
              agent_key="dispatch")
    if commit:
        db.commit()
    else:
        db.flush()
    return out


# ---------------------------------------------------------------------------
# 协商状态（S3）
# ---------------------------------------------------------------------------
def negotiation_of(t: Tender) -> Dict[str, Any]:
    """取协商轨迹。历史招标单没有这一列，按空轨迹处理，不报错。"""
    n = t.negotiation if isinstance(t.negotiation, dict) else {}
    n = dict(n)
    n.setdefault("round", int(t.round_no or 1))
    n.setdefault("turns", [])
    n.setdefault("outcome", None)
    return n


def _save_neg(t: Tender, neg: Dict[str, Any]) -> None:
    t.negotiation = _jsonable(neg)
    if neg.get("round"):
        t.round_no = int(neg["round"])


def counter_timeout_s(db: Session) -> int:
    try:
        return max(30, int(str(runtime.get(db, "tender.counter_timeout_s") or 600)))
    except (TypeError, ValueError):
        return 600


def open_negotiation(t: Tender, *, target_org_id: int, max_rounds: int,
                     amount: Any) -> Dict[str, Any]:
    """初始化/推进协商轨迹。第一轮由调度出价，之后每轮覆盖 round。"""
    neg = negotiation_of(t)
    neg["target_org_id"] = int(target_org_id)
    neg["max_rounds"] = int(max_rounds)
    neg["round"] = int(neg.get("round") or 1)
    neg["rounds_used"] = int(neg.get("rounds_used") or 0) + 1
    neg["awaiting_since"] = _now().isoformat(timespec="seconds")
    neg["turns"].append({"round": neg["round"], "from": "dispatch",
                         "type": "counter", "amount": _num(amount),
                         "at": _now().isoformat(timespec="seconds"),
                         "reason": "重申本单可付上限"})
    _save_neg(t, neg)
    return neg


def award_by_accept(db: Session, order: ShipperOrder, t: Tender, bid: CarrierBid, *,
                    amount: Any, round_no: int, user: Dict[str, Any],
                    note: str = "", commit: bool = True) -> Dict[str, Any]:
    """协商达成一致后定标。**金额不会超过招标上限**，所以不破坏金额链。"""
    neg = negotiation_of(t)
    neg.update({"outcome": "awarded", "final_amount": _num(amount),
                "round": int(round_no), "awaiting_since": None})
    _save_neg(t, neg)

    evidence = dict(t.arbitration or {})
    evidence["outcome"] = {"type": "negotiated", "final_amount": _num(amount),
                           "round": int(round_no),
                           "at": _now().isoformat(timespec="seconds")}
    t.arbitration = _jsonable(evidence)

    _apply_award(db, order, t, bid_id=bid.id, carrier_org_id=bid.carrier_org_id,
                 quote=amount, score=None, user=user,
                 note=f"协商第 {round_no} 轮达成，成交价 ¥{_q(amount)}")
    add_event(db, order.id, "tender_negotiated", "dispatcher", user.get("user_id"),
              {"tender_id": t.id, "carrier_org_id": bid.carrier_org_id,
               "amount": _num(amount), "round": int(round_no)})
    audit.log(db, user, "tender.negotiate_award", target_type="order",
              target_id=order.id, reason=note,
              detail={"tender_id": t.id, "amount": _num(amount),
                      "round": int(round_no)},
              agent_key="dispatch")
    if commit:
        db.commit()
    else:
        db.flush()
    return {"tender_id": t.id, "status": t.status,
            "status_label": TENDER_STATUS_LABEL[t.status],
            "amount": _num(amount), "round": int(round_no),
            "carrier_org_id": bid.carrier_org_id, "negotiation": neg}


def fail_negotiation(db: Session, order: ShipperOrder, t: Tender, *,
                     reason: str, user: Optional[Dict[str, Any]] = None,
                     outcome: str = "failed", gap: Optional[Any] = None,
                     commit: bool = True) -> Dict[str, Any]:
    """协商无果。订单留在 published，等人工决定提价 / 改期 / 换承运商。

    **不自动提价**：改 ``budget_max`` 是 ``HUMAN_REQUIRED.price_increase``。
    """
    neg = negotiation_of(t)
    neg.update({"outcome": outcome, "reason": reason, "awaiting_since": None})
    if gap is not None:
        neg["gap"] = _num(gap)
    _save_neg(t, neg)

    t.status = "failed"
    t.closed_at = _now()
    add_event(db, order.id, "tender_failed", "dispatcher",
              (user or {}).get("user_id"),
              {"tender_id": t.id, "reason": reason,
               "negotiation_outcome": outcome})
    if user:
        audit.log(db, user, "tender.negotiate_failed", target_type="order",
                  target_id=order.id, reason=reason,
                  detail={"tender_id": t.id, "outcome": outcome},
                  agent_key="dispatch")
    if commit:
        db.commit()
    else:
        db.flush()
    return {"tender_id": t.id, "status": t.status,
            "status_label": TENDER_STATUS_LABEL[t.status],
            "reason": reason, "negotiation": neg}


def sweep_due(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """把收标已截止的招标单裁决掉。供 dispatch daemon 的 ``due()`` 定时调用。

    为什么放在服务层而不是 daemon 里：``到期就裁决`` 是招标的规则，不是调度的
    判断。放进 daemon，规则就跟着进程走——换一个入口（比如以后的 worker 集群）
    就得再抄一遍，抄漏了就是"有的单到点不裁决"。

    两个容易踩的坑，都在这一个查询里处理掉了：

    * **必须 join 订单**。只查 tender 的话，订单被删掉的历史招标单会占满
      ``limit``，排在后面的真单永远轮不到——队列"看起来在跑"，实际一件没做。
    * **孤儿招标单要收掉**。订单没了的招标单不会自己消失，不清理就会越积越多。
      它们已经不可能成交，统一置 ``cancelled``，理由是数据清理，不是业务失败。
    """
    now = _now()
    rows = db.execute(
        select(Tender).join(ShipperOrder, ShipperOrder.id == Tender.order_id)
        .where(Tender.status == "open",
               Tender.deadline_at.isnot(None),
               Tender.deadline_at <= now)
        .order_by(Tender.id).limit(limit)).scalars().all()
    out: List[Dict[str, Any]] = []
    agent = {"user_id": 0, "account": "dispatch:agent", "role": "dispatcher"}
    for t in rows:
        order = db.get(ShipperOrder, t.order_id)
        if not order:
            continue
        out.append(close_tender(db, order, agent, commit=False))

    # 孤儿清理：单独一条查询，不占上面那 20 个名额
    orphans = db.execute(
        select(Tender).where(Tender.status == "open",
                             Tender.deadline_at.isnot(None),
                             Tender.deadline_at <= now,
                             ~Tender.order_id.in_(select(ShipperOrder.id)))
        .order_by(Tender.id).limit(limit)).scalars().all()
    for t in orphans:
        t.status = "cancelled"
        t.closed_at = now
    if out or orphans:
        db.commit()
    return out


def expire_stale(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """扫描协商超时的招标单并判死。供 S4 的 daemon 定时调用。"""
    timeout = counter_timeout_s(db)
    now = _now()
    out: List[Dict[str, Any]] = []
    rows = db.execute(select(Tender).where(
        Tender.status == "negotiating").order_by(Tender.id).limit(limit)).scalars().all()
    for t in rows:
        neg = negotiation_of(t)
        stamp = neg.get("awaiting_since")
        if not stamp:
            continue
        try:
            since = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if (now - since).total_seconds() <= timeout:
            continue
        order = db.get(ShipperOrder, t.order_id)
        if not order:
            continue
        out.append(fail_negotiation(
            db, order, t, user=None, outcome="timeout",
            reason=f"协商超过 {timeout} 秒未收到回应，判无交集交人", commit=False))
    if out:
        db.commit()
    return out


# ---------------------------------------------------------------------------
# 视图（按角色裁剪）
# ---------------------------------------------------------------------------
def view(db: Session, order_id: int, *, viewer_org: Optional[int] = None) -> Dict[str, Any]:
    """招标视图。``viewer_org`` 有值＝承运商视角，**看不到别家的报价**。

    别家报了多少是竞争对手的信息。竞价规则要求知道"有哪些人在争"，
    但不要求知道"他们报了多少"——后者会让后报的人照着压价，
    竞价就变成了抢尾盘。
    """
    t = latest_tender(db, order_id)
    if t is None:
        return {"ok": False, "reason": "这单还没有招标记录"}
    bids = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == int(order_id)).order_by(CarrierBid.id)).scalars().all()
    base = {"ok": True, "tender_id": t.id, "status": t.status,
            "status_label": TENDER_STATUS_LABEL.get(t.status, t.status),
            "round_no": int(t.round_no or 1), "budget": _num(t.budget),
            "deadline_at": t.deadline_at.isoformat(timespec="seconds") if t.deadline_at else None,
            "bid_count": len([b for b in bids if b.quote_amount is not None]),
            "winner_bid_id": t.winner_bid_id,
            "rule_version": (t.arbitration or {}).get("rule_version"),
            "weights": (t.arbitration or {}).get("weights")}

    if viewer_org is not None:
        neg = negotiation_of(t)
        is_target = int(neg.get("target_org_id") or 0) == int(viewer_org)
        # 只有被还价的那家能看到协商轨迹：别家看到的"有人被还价了"就是
        # 竞争对手的谈判筹码，公开它等于泄露定价空间。
        if is_target:
            base["negotiation"] = {
                "round": int(neg.get("round") or 1),
                "max_rounds": int(neg.get("max_rounds") or 0),
                "rounds_used": int(neg.get("rounds_used") or 0),
                "outcome": neg.get("outcome"),
                "turns": neg.get("turns") or [],
            }
        mine = next((b for b in bids if b.carrier_org_id == viewer_org
                     and b.quote_amount is not None), None)
        base["my_bid"] = ({"bid_id": mine.id, "quote": _num(mine.quote_amount),
                           "eta_h": _num(mine.quote_eta_h or 0),
                           "round": mine.bid_round, "decision": mine.decision,
                           "note": mine.bid_note,
                           # 协商成交时"我报了多少"和"最后成交多少"是两个数，
                           # 只给前者会让人以为自己中了别人的价
                           "final_quote": (_num(neg.get("final_amount"))
                                           if is_target
                                           and neg.get("outcome") == "awarded" else None),
                           "history": mine.bid_history or []} if mine else None)
        base["won"] = bool(mine and t.winner_bid_id == mine.id)
        if t.status in ("awarded", "failed", "cancelled"):
            base["result"] = ("你中标了" if base["won"]
                              else "本单已定标" if t.status == "awarded" else "本单流标")
        return base

    arb = t.arbitration or {}
    verdicts = {c.get("bid_id"): c for c in (arb.get("candidates") or [])}
    names = _org_names(db, [b.carrier_org_id for b in bids])
    # 评分是承运商的档案属性，裁决前也该看得到——否则收标中的比价表
    # 一整列都是"—"，看的人以为数据没取到。综合分才依赖裁决。
    ratings: Dict[int, float] = {}
    if bids:
        for p in db.execute(select(CarrierProfile).where(
                CarrierProfile.org_id.in_([b.carrier_org_id for b in bids]))).scalars().all():
            if p.org_id and p.rating is not None:
                ratings[int(p.org_id)] = _num(p.rating)
    rows = [{
        "bid_id": b.id, "carrier_org_id": b.carrier_org_id,
        # 名字与评分是"为什么是这家"的一半：只有报价的比价表看不出
        # 便宜的那家为什么会被淘汰（可能是时效或空驶拖了分）。
        "carrier_org": names.get(b.carrier_org_id),
        "rating": ((verdicts.get(b.id) or {}).get("rating")
                   if (verdicts.get(b.id) or {}).get("rating") is not None
                   else ratings.get(b.carrier_org_id)),
        "quote": _num(b.quote_amount) if b.quote_amount is not None else None,
        "eta_h": _num(b.quote_eta_h) if b.quote_eta_h is not None else None,
        "deadhead_km": _num(b.deadhead_km) if b.deadhead_km is not None else None,
        "round": b.bid_round, "decision": b.decision, "note": b.bid_note,
        "score": (verdicts.get(b.id) or {}).get("score"),
        "verdict": (verdicts.get(b.id) or {}).get("verdict"),
        "reason": (verdicts.get(b.id) or {}).get("reason"),
        "reason_code": (verdicts.get(b.id) or {}).get("reason_code"),
        "final_quote": None,
    } for b in bids]

    # 协商成交的单，裁决证据里**没有 winner 这一条**：``award_by_accept`` 只写
    # ``outcome.type = negotiated``，``candidates`` 仍停在收标时"超预算"的结论。
    # 直接按 verdict 渲染会把中标方标成"淘汰"——最该被解释的那一行反而是错的。
    # 这里只补显示层，不改库：收标时的原报价与原因都留着，成交价另给一列。
    outcome = arb.get("outcome") if isinstance(arb.get("outcome"), dict) else {}
    if outcome.get("type") == "negotiated" and t.winner_bid_id:
        final = _num(outcome.get("final_amount"))
        rnd = int(outcome.get("round") or 1)
        for row in rows:
            if int(row["bid_id"]) != int(t.winner_bid_id):
                continue
            # 收标那一刻的报价在裁决证据里：承运商接受还价后会把
            # ``carrier_bid.quote_amount`` 改成成交价，行上的数字已经不是原报价了。
            # 拿行上的数字当原报价，会写出"报价 ¥12740 超上限 ¥12740"这种胡话。
            orig = (verdicts.get(row["bid_id"]) or {}).get("quote")
            orig = _num(orig) if orig is not None else row["quote"]
            over = ""
            if orig is not None and t.budget is not None and _q(orig) > _q(t.budget):
                over = f"（收标时报价 ¥{_q(orig)} 超可付上限 ¥{_q(t.budget)}）"
            row.update({
                "verdict": "winner", "reason_code": "negotiated",
                "final_quote": final,
                "reason": f"协商第 {rnd} 轮以 ¥{_q(final)} 成交{over}",
            })
    base["bids"] = rows
    base["final_amount"] = _num(outcome.get("final_amount")) if outcome else None
    base["arbitration"] = arb or None
    neg = negotiation_of(t)
    base["negotiation"] = {
        "round": int(neg.get("round") or 1),
        "max_rounds": int(neg.get("max_rounds") or 0),
        "rounds_used": int(neg.get("rounds_used") or 0),
        "target_org_id": neg.get("target_org_id"),
        "outcome": neg.get("outcome"),
        "final_amount": neg.get("final_amount"),
        "gap": neg.get("gap"),
        "reason": neg.get("reason"),
        "awaiting_since": neg.get("awaiting_since"),
        "turns": neg.get("turns") or [],
    }
    return base


__all__ = ["TenderError", "TENDER_MINUTES", "open_tender", "submit_bid",
           "close_tender", "view", "active_tender", "latest_tender",
           "weights_of", "max_rounds_of", "freight_floor", "required_eta_h",
           "TENDER_STATUS_LABEL", "board",
           # S3 协商
           "negotiation_of", "open_negotiation", "award_by_accept",
           "fail_negotiation", "expire_stale", "counter_timeout_s"]
