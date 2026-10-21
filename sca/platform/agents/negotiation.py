"""协商往复：调度还价 ↔ 承运商回价（S3）。

这个文件只做一件事：**把一次还价变成两条消息**。判断规则全在
``services/tender.py``（``dispatch_reply`` / ``carrier_reply``），落库全在
``services/tender_ops.py``。这里不写第三条规则——规则散在三个地方，
就再也说不清"为什么最后是这个价"。

两个 handler 的签名是 **S4 daemon 的接口**：

    carrier_daemon.handle(msg)   -> carrier_handle_counter(db, msg)
    dispatch_daemon.handle(msg)  -> dispatch_handle_counter(db, msg)

``advance()`` 是 S3 的同步替身：daemon 还没起来，用它在一次 HTTP 请求里把
消息推完。S4 之后它只留给测试与"手动催一下"，不再是生产主路径。

金额口径（别在这里改）
----------------------
调度的出价上限永远是 ``Tender.budget``（等于 ``publish_price``）。
它**没有加价权**——加价等于改 ``budget_max``，那是
``contract.HUMAN_REQUIRED.price_increase``。所以调度的还价只有"重申上限"
这一个动作，谈不动就交人。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CarrierBid, ShipperOrder, Tender
from ..services import audit, notify, runtime, tender_ops
from ..services import tender as rules
from ..services.orders import add_event
from . import bus

NEG_TYPES = ("counter", "accept", "reject")


def carrier_key(org_id: Any) -> str:
    return f"carrier:{int(org_id)}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _order_id_of(msg) -> Optional[int]:
    p = msg.payload or {}
    if p.get("order_id"):
        try:
            return int(p["order_id"])
        except (TypeError, ValueError):
            return None
    tid = str(msg.thread_id or "")
    if tid.startswith("order:"):
        try:
            return int(tid.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def _floor_of(db: Session, bid: CarrierBid) -> Any:
    """承运商的成本底线（运费口径）。

    优先用投标时算好的那个数——当时的位置、费率、里程都在
    ``bid_history`` 里，事后重算会因为位置变了而给出不同的底线，
    那就不是"我当时能不能接"而是"现在能不能接"了。
    """
    hist = bid.bid_history or []
    if hist and isinstance(hist[-1], dict) and hist[-1].get("floor") is not None:
        return hist[-1]["floor"]
    if bid.est_cost is None:
        return 0
    _, cr = runtime.active_fee(db)
    return tender_ops.freight_floor(bid.est_cost, cr)


def _target_bid(db: Session, t: Tender) -> Optional[CarrierBid]:
    """还价对象：超预算的投标里报价最低的那家。

    从 ``tender.arbitration.candidates`` 里取，而不是重新跑一遍裁决：
    还价对象必须是"裁决当时算出来的那个"，否则人看到的证据和实际动作对不上。
    """
    arb = t.arbitration if isinstance(t.arbitration, dict) else {}
    over = [c for c in (arb.get("candidates") or [])
            if c.get("reason_code") == "over_budget" and c.get("bid_id")]
    if over:
        pick = min(over, key=lambda c: (float(c.get("quote") or 0), int(c.get("bid_id") or 0)))
        return db.get(CarrierBid, int(pick["bid_id"]))

    rows = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == t.order_id,
        CarrierBid.decision.in_(("bid", "candidate")),
        CarrierBid.quote_amount.isnot(None))).scalars().all()
    above = [b for b in rows if b.quote_amount is not None and b.quote_amount > t.budget]
    return min(above, key=lambda b: b.quote_amount) if above else None


def _record(t: Tender, turn: Dict[str, Any]) -> Dict[str, Any]:
    neg = tender_ops.negotiation_of(t)
    neg.setdefault("turns", []).append(turn)
    neg["awaiting_since"] = turn.get("at")
    t.negotiation = _jsonable(neg)
    return neg


def _jsonable(v: Any) -> Any:
    from datetime import datetime
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 调度 -> 承运商：还价
# ---------------------------------------------------------------------------
def send_counter(db: Session, order: ShipperOrder, t: Tender, bid: CarrierBid, *,
                 user: Optional[Dict[str, Any]] = None,
                 commit: bool = False) -> Dict[str, Any]:
    """调度出价（= 重申上限）。每调用一次算一轮。"""
    if t.status != "negotiating":
        raise tender_ops.TenderError(
            f"招标当前状态「{t.status}」不在协商中，不能还价")

    max_rounds = tender_ops.max_rounds_of(db)
    neg = tender_ops.negotiation_of(t)
    if int(neg.get("rounds_used") or 0) >= max_rounds:
        return tender_ops.fail_negotiation(
            db, order, t, user=user, commit=commit,
            reason=f"已用满 {max_rounds} 轮还价，仍未达成一致")

    amount = t.budget
    neg = tender_ops.open_negotiation(t, target_org_id=bid.carrier_org_id,
                                      max_rounds=max_rounds, amount=amount)
    db.flush()

    msg = bus.send(
        db, from_agent="dispatch", to_agent=carrier_key(bid.carrier_org_id),
        msg_type="counter",
        payload={"amount": float(amount),
                 "reason": f"本单可付上限 ¥{amount}，能否按此价承运",
                 "round": int(neg["round"]), "tender_id": t.id,
                 "order_id": order.id,
                 "deadline_at": (t.deadline_at.isoformat(timespec="seconds")
                                 if t.deadline_at else None)},
        order_id=order.id)

    add_event(db, order.id, "tender_counter_sent", "dispatcher",
              (user or {}).get("user_id"),
              {"tender_id": t.id, "to_org": bid.carrier_org_id,
               "amount": float(amount), "round": int(neg["round"]),
               "msg_id": msg.id})
    if bid.carrier_user_id:
        notify.push(db, bid.carrier_user_id, ntype="negotiation",
                    title=f"调度还价 ¥{amount}（第 {neg['round']} 轮）",
                    body=f"订单 {order.order_no}：{order.origin}→{order.dest}，"
                         f"请确认能否按此价承运",
                    order_id=order.id)
    audit.log(db, user or {"user_id": 0, "account": "dispatch", "role": "dispatcher"},
              "tender.counter", target_type="order", target_id=order.id,
              detail={"tender_id": t.id, "amount": float(amount),
                      "round": int(neg["round"])},
              agent_key="dispatch")
    if commit:
        db.commit()
    else:
        db.flush()
    return {"tender_id": t.id, "round": int(neg["round"]),
            "amount": float(amount), "msg_id": msg.id}


# ---------------------------------------------------------------------------
# 承运商：回价 / 接受 / 退出
# ---------------------------------------------------------------------------
def carrier_handle_counter(db: Session, msg) -> Optional[Dict[str, Any]]:
    """承运商收到调度还价。返回自己的决定，并回一条消息。"""
    p = msg.payload or {}
    order_id = _order_id_of(msg)
    if not order_id or not p.get("tender_id"):
        return None
    order = db.get(ShipperOrder, order_id)
    t = db.get(Tender, int(p["tender_id"]))
    if not order or not t or t.status != "negotiating":
        return None

    org = int(str(msg.to_agent).split(":", 1)[1])
    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == org)).scalars().first()
    if not bid or bid.decision not in ("bid", "candidate"):
        return None

    floor = _floor_of(db, bid)
    own = bid.quote_amount or 0
    neg = tender_ops.negotiation_of(t)
    reply = rules.carrier_reply(counter_amount=p.get("amount"), floor_price=floor,
                                own_quote=own, round_no=int(neg.get("round") or 1),
                                max_rounds=int(neg.get("max_rounds") or rules.MAX_ROUNDS))

    action = reply["action"]
    amount = reply.get("amount")
    _record(t, {"round": int(neg.get("round") or 1), "from": "carrier",
                "type": action,
                "amount": float(amount) if amount is not None else None,
                "at": _now_iso(), "reason": reply.get("reason")})

    if action in ("counter", "accept") and amount is not None:
        bid.quote_amount = amount
        bid.quote_eta_h = bid.quote_eta_h or 0
        hist = list(bid.bid_history or [])
        hist.append({"round": int(neg.get("round") or 1),
                     "amount": float(amount), "eta_h": float(bid.quote_eta_h or 0),
                     "at": _now_iso(), "auto": True, "floor": float(floor),
                     "source": "counter_reply"})
        bid.bid_history = hist
        bid.bid_round = int(neg.get("round") or 1)
    if action == "reject":
        bid.decision = "withdrawn"

    msg_type = {"accept": "accept", "counter": "counter", "reject": "reject"}[action]
    payload: Dict[str, Any] = {"reason": reply.get("reason") or action,
                               "tender_id": t.id, "order_id": order_id,
                               "round": int(neg.get("round") or 1)}
    if msg_type == "counter":
        payload["amount"] = float(amount)
    elif msg_type == "accept":
        payload["amount"] = float(amount)

    sent = bus.send(db, from_agent=carrier_key(org), to_agent="dispatch",
                    msg_type=msg_type, payload=payload, order_id=order_id,
                    parent_id=msg.id)
    add_event(db, order_id, "tender_counter_reply", "carrier",
              bid.carrier_user_id,
              {"tender_id": t.id, "action": action,
               "amount": float(amount) if amount is not None else None,
               "floor": float(floor), "msg_id": sent.id})
    bus.ack(db, msg.id, ok=True)
    db.flush()
    return {"action": action, "amount": amount, "msg_id": sent.id,
            "reason": reply.get("reason")}


# ---------------------------------------------------------------------------
# 调度：收到回价后定标或交人
# ---------------------------------------------------------------------------
def dispatch_handle_counter(db: Session, msg) -> Optional[Dict[str, Any]]:
    """调度收到承运商的回价/接受/退出。"""
    p = msg.payload or {}
    order_id = _order_id_of(msg)
    if not order_id or not p.get("tender_id"):
        return None
    order = db.get(ShipperOrder, order_id)
    t = db.get(Tender, int(p["tender_id"]))
    if not order or not t or t.status != "negotiating":
        return None

    org = int(str(msg.from_agent).split(":", 1)[1])
    bid = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == org)).scalars().first()
    if not bid:
        return None

    neg = tender_ops.negotiation_of(t)
    round_no = int(neg.get("round") or 1)
    user = {"user_id": 0, "account": "dispatch:agent", "role": "dispatcher"}

    if msg.msg_type == "accept":
        tender_ops.award_by_accept(
            db, order, t, bid, amount=p.get("amount") or bid.quote_amount,
            round_no=round_no, user=user, commit=False,
            note=f"承运商接受还价 ¥{p.get('amount')}")
        bus.ack(db, msg.id, ok=True)
        return {"outcome": "awarded", "tender_id": t.id}

    if msg.msg_type == "reject":
        # 承运商退出与"无交集"在业务上是两件事，但对人的处置动作是同一个：
        # 订单留在 published，等人决定提价 / 改期 / 换承运商。所以走同一条出口，
        # 理由里保留它自己说的话，不替它解释。
        gap = max(0.0, float(bid.quote_amount or 0) - float(t.budget or 0))
        return _no_deal(db, order, t, bid, msg, user=user,
                        reason=f"承运商退出协商：{p.get('reason') or '未说明'}"
                               + (f"（缺口 ¥{gap:.2f}）" if gap else ""),
                        gap=gap)

    # counter：承运商回价
    verdict = rules.dispatch_reply(offer=t.budget, counter=p.get("amount"),
                                   round_no=round_no,
                                   max_rounds=int(neg.get("max_rounds") or rules.MAX_ROUNDS))

    if verdict["action"] == "counter":
        # 轮次与金额由 send_counter 统一记一条，避免同一轮出现两条 dispatch 痕迹
        send_counter(db, order, t, bid, user=user, commit=False)
        bus.ack(db, msg.id, ok=True)
        return {"outcome": "countering", "tender_id": t.id}

    if verdict["action"] == "accept":
        _record(t, {"round": round_no, "from": "dispatch", "type": "accept",
                    "amount": float(verdict["amount"]), "at": _now_iso(),
                    "reason": verdict.get("reason")})
        tender_ops.award_by_accept(
            db, order, t, bid, amount=verdict["amount"], round_no=round_no,
            user=user, commit=False, note=verdict.get("reason") or "")
        bus.ack(db, msg.id, ok=True)
        return {"outcome": "awarded", "tender_id": t.id}

    # 无交集：留痕 + 升级给货主（提价属人审项，agent 只建议）
    gap = max(0.0, float(p.get("amount") or 0) - float(t.budget or 0))
    return _no_deal(db, order, t, bid, msg, user=user, gap=gap,
                    reason=verdict.get("reason") or "协商无交集")


def _no_deal(db: Session, order: ShipperOrder, t: Tender, bid: CarrierBid, msg, *,
             user: Optional[Dict[str, Any]], reason: str,
             gap: float = 0.0) -> Dict[str, Any]:
    """协商结束但没成交：一条出口，四个动作一个都不能少。

    1. 轨迹里记一条 dispatch/fail（否则回放时只看到"承运商拒绝了"，看不到裁决）
    2. 招标单进 failed 终态
    3. 向货主 escalate（**只建议**：提价属 ``price_increase``，人审项）
    4. 站内通知 + ack

    缺任何一步，"为什么这单没成"就说不清了。
    """
    neg = tender_ops.negotiation_of(t)
    round_no = int(neg.get("round") or 1)
    _record(t, {"round": round_no, "from": "dispatch", "type": "fail",
                "amount": None, "at": _now_iso(), "reason": reason})

    out = tender_ops.fail_negotiation(db, order, t, user=user, commit=False,
                                      gap=gap, reason=reason)
    try:
        bus.send(db, from_agent="dispatch", to_agent="shipper",
                 msg_type="escalate",
                 payload={"reason": f"协商无果：{reason}",
                          "ask": "是否提高预算 / 延后时效 / 更换承运商",
                          "gap": gap, "tender_id": t.id, "order_id": order.id},
                 order_id=order.id)
    except Exception:          # 总线不可用不该掩盖"协商已判死"这个事实
        pass
    if order.shipper_user_id:
        notify.push(db, order.shipper_user_id, ntype="negotiation",
                    title=f"订单 {order.order_no} 协商无果，需人工决定",
                    body=(f"最低可接受报价比可付上限高 ¥{gap:.2f}，" if gap else "")
                         + "可考虑提高预算、延后时效或更换承运商",
                    order_id=order.id)
    bus.ack(db, msg.id, ok=True)
    return {"outcome": "failed", "tender_id": t.id, "gap": gap,
            "reason": out.get("reason")}


# ---------------------------------------------------------------------------
# 到期检查（S4 daemon 的 due() 调这两个）
# ---------------------------------------------------------------------------
def start_negotiation(db: Session, t: Tender, *,
                      user: Optional[Dict[str, Any]] = None,
                      commit: bool = False) -> Dict[str, Any]:
    """招标刚进入协商时，向超标最低报价者发出第一轮还价。

    ``close_tender`` 只负责把状态改成 negotiating，它不知道"该找谁谈"——
    挑对象是调度的事（谁超预算、谁最便宜），所以放在这里。
    """
    order = db.get(ShipperOrder, t.order_id)
    if order is None:
        return {"outcome": "skipped", "reason": "订单不存在"}
    if t.status != "negotiating":
        return {"outcome": "skipped", "reason": f"招标状态 {t.status} 不是协商中"}

    target = _target_bid(db, t)
    if target is None:
        return tender_ops.fail_negotiation(
            db, order, t, user=user, commit=commit,
            reason="没有可协商的投标（无超预算报价）")
    return send_counter(db, order, t, target, user=user, commit=commit)


def sweep_timeouts(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """协商超时的招标单：判死 + 升级货主。

    ``tender_ops.expire_stale`` 已经把"判死 + 留痕"做完了，但它是服务层，
    不该发消息。缺的那两步（escalate 给货主、站内通知）在这里补上——
    否则超时的单子会静悄悄地变成 failed，货主永远不知道有人在等他决定。
    """
    out = tender_ops.expire_stale(db, limit=limit)
    for r in out:
        t = db.get(Tender, int(r.get("tender_id") or 0))
        if t is None:
            continue
        order = db.get(ShipperOrder, t.order_id)
        if order is None:
            continue
        gap = float((t.negotiation or {}).get("gap") or 0)
        try:
            bus.send(db, from_agent="dispatch", to_agent="shipper",
                     msg_type="escalate",
                     payload={"reason": r.get("reason") or "协商超时",
                              "ask": "是否提高预算 / 延后时效 / 更换承运商",
                              "gap": gap, "tender_id": t.id, "order_id": order.id},
                     order_id=order.id)
        except Exception:                       # noqa: BLE001
            pass
        notify.push(db, order.shipper_user_id, ntype="negotiation",
                    title=f"订单 {order.order_no} 协商超时，需人工决定",
                    body="承运商在约定时间内没有回应，招标已判流标。",
                    order_id=order.id)
    if out:
        db.commit()
    return out


# ---------------------------------------------------------------------------
# S3 同步驱动（S4 由 daemon 替换）
# ---------------------------------------------------------------------------
def _claim_for(db: Session, agent_key: str, order_id: int,
               limit: int = 20) -> List[Any]:
    """只领属于本单的协商消息；别的消息原样退回 pending。

    ``bus.claim`` 会把领到的全部标 consumed，所以这里必须把不相关的一条条
    退回去——顺手把别的单的消息吃掉，是比"没处理"严重得多的错误。
    """
    out: List[Any] = []
    for m in bus.claim(db, agent_key, limit=limit):
        if m.msg_type in NEG_TYPES and _order_id_of(m) == int(order_id):
            out.append(m)
        else:
            m.status = "pending"
            m.consumed_at = None
    db.flush()
    return out


def advance(db: Session, order: ShipperOrder, t: Tender, *,
            user: Optional[Dict[str, Any]] = None,
            max_steps: int = 12, commit: bool = True) -> Dict[str, Any]:
    """把本单的协商往前推，直到需要外部输入或到终态。"""
    if t.status != "negotiating":
        return {"tender_id": t.id, "status": t.status,
                "status_label": tender_ops.TENDER_STATUS_LABEL.get(t.status, t.status),
                "negotiation": tender_ops.negotiation_of(t)}

    neg = tender_ops.negotiation_of(t)
    if not neg.get("turns"):
        target = _target_bid(db, t)
        if target is None:
            tender_ops.fail_negotiation(db, order, t, user=user, commit=False,
                                        reason="没有可协商的投标（无超预算报价）")
            if commit:
                db.commit()
            return {"tender_id": t.id, "status": t.status,
                    "status_label": tender_ops.TENDER_STATUS_LABEL.get(t.status, t.status),
                    "negotiation": tender_ops.negotiation_of(t)}
        send_counter(db, order, t, target, user=user, commit=False)
        neg = tender_ops.negotiation_of(t)

    steps = 0
    while steps < max_steps and t.status == "negotiating":
        moved = False

        for m in _claim_for(db, "dispatch", order.id):
            dispatch_handle_counter(db, m)
            steps += 1
            moved = True
            if t.status != "negotiating":
                break
        if t.status != "negotiating":
            break

        org = int(tender_ops.negotiation_of(t).get("target_org_id") or 0)
        for m in _claim_for(db, carrier_key(org), order.id):
            carrier_handle_counter(db, m)
            steps += 1
            moved = True
            if t.status != "negotiating":
                break
        if not moved:
            break

    if commit:
        db.commit()
    else:
        db.flush()
    neg = tender_ops.negotiation_of(t)
    return {"tender_id": t.id, "status": t.status,
            "status_label": tender_ops.TENDER_STATUS_LABEL.get(t.status, t.status),
            "round": int(neg.get("round") or 1),
            "rounds_used": int(neg.get("rounds_used") or 0),
            "outcome": neg.get("outcome"),
            "final_amount": neg.get("final_amount"),
            "gap": neg.get("gap"),
            "reason": neg.get("reason"),
            "turns": neg.get("turns") or [],
            "negotiation": neg}


def tick(db: Session, agent_key: str, *, limit: int = 20,
         commit: bool = True) -> Dict[str, Any]:
    """某个 agent 跑一轮自己的收件箱。S4 daemon 的 ``tick()`` 就是调它。

    这是"每个 agent 有独立循环"的最小可运行形态：调度 tick 只处理发给
    ``dispatch`` 的消息，承运商 tick 只处理发给 ``carrier:{org}`` 的消息，
    两者互不调用。S3 还没有常驻进程，所以由 HTTP 或测试脚本触发。
    """
    handled: List[Dict[str, Any]] = []
    skipped = 0
    for m in bus.claim(db, agent_key, limit=limit):
        try:
            if m.msg_type in NEG_TYPES:
                fn = (dispatch_handle_counter if agent_key == "dispatch"
                      else carrier_handle_counter)
                r = fn(db, m)
                if r is None:
                    # 不是协商消息或状态已变（比如招标已定标）：记 ack 不重投，
                    # 否则一条过期消息会被永远反复领取。
                    bus.ack(db, m.id, ok=True, error="stale")
                    skipped += 1
                else:
                    handled.append({"msg_id": m.id, **{k: v for k, v in r.items()
                                                       if k != "msg_id"}})
            else:
                m.status = "pending"
                m.consumed_at = None
                skipped += 1
        except Exception as exc:                      # 单条失败不拖垮整轮
            bus.ack(db, m.id, ok=False, error=f"{type(exc).__name__}: {exc}"[:200])
    if commit and handled:
        db.commit()
    elif commit:
        db.flush()
    return {"agent": agent_key, "handled": len(handled), "skipped": skipped,
            "results": handled}


__all__ = ["carrier_key", "send_counter", "carrier_handle_counter",
           "dispatch_handle_counter", "start_negotiation", "sweep_timeouts",
           "advance", "tick"]
