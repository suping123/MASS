"""承运商 Agent：自己的成本自己算，自己的价自己出。

它跟调度最容易搞混的地方：**它看到的订单里没有 ``budget_max``**。
所以它不可能"照着预算报价"——它只知道自己的每公里成本、空驶距离、目标利润率，
报出来的价是"我干这活值多少"，不是"平台最多能给多少"。两边信息不对称，
才有真实的讨价还价；信息一对称，定价就又是一句话的事。

收件箱按组织隔离：``carrier:{org_id}``。一个进程服务多个组织，
但每条的 ``to_agent`` 就是它的身份——它读不到另一个 org 的收件箱，
也读不到另一个 org 的私有记忆（``agent_memory`` 按 agent_key 分区）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AgentActor, AgentMessage, CarrierBid, CarrierProfile, Tender
from ...services import audit, carrier_ops, tender_ops
from .. import bus, contract, handoff, negotiation, views
from ..daemon import AgentDaemon

KIND = "carrier"
AGENT_KEY = "carrier"          # 单例形态（测试/旧消息）；真实收件箱是 carrier:{org}

# 目标权重：这一单能赚多少是第一位的，其次是车别闲着。与货主天然对立。
UTILITY = {"profit": 0.6, "utilization": 0.3, "risk": 0.1}


def key_for(org_id: Any) -> str:
    return negotiation.carrier_key(org_id)


def _org_of(agent_key: str) -> Optional[int]:
    try:
        return int(str(agent_key).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _on_negotiation(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """调度还价 → 承运商自己决定接受 / 回价 / 退出（规则在 tender.carrier_reply）。"""
    return negotiation.carrier_handle_counter(db, msg)


def _on_request(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """能回答的只有"我自己的"报价口径，跨到别人的一律拒绝。"""
    p = msg.payload or {}
    fields = [str(f) for f in (p.get("fields") or [])]
    mine = views.visible_fields("carrier", "bid")
    theirs = views.visible_fields(contract.kind_of(msg.from_agent), "bid")
    bad = [f for f in fields if f not in mine or f not in theirs]
    if bad:
        bus.reply(db, msg, "reject",
                  {"reason": f"越权取数：{', '.join(bad)} 不属于承运商可提供字段"})
        return {"action": "rejected", "fields": bad}
    bus.reply(db, msg, "notify",
              {"event": "报价口径可提供", "fields": {k: "见承运商档案" for k in fields}})
    return {"action": "answered", "fields": fields}


# ---------------------------------------------------------------------------
# 人类委托（S5）：接单 / 投标
# ---------------------------------------------------------------------------
def _user_of(args: Dict[str, Any]) -> Dict[str, Any]:
    """从消息里还原"谁在操作"。人是把身份带进消息里的，不是从会话猜的。"""
    u = dict(args.get("user") or {})
    return {"user_id": int(u.get("user_id") or 0), "org_id": u.get("org_id"),
            "role": "carrier", "account": str(u.get("account") or "carrier")}


def _do_accept(db: Session, msg: AgentMessage, args: Dict[str, Any]) -> Dict[str, Any]:
    return carrier_ops.accept_core(db, _user_of(args), int(args.get("order_id") or 0),
                                   str(args.get("position_text") or ""))


def _do_bid(db: Session, msg: AgentMessage, args: Dict[str, Any]) -> Dict[str, Any]:
    return tender_ops.submit_bid(
        db, _user_of(args), int(args.get("order_id") or 0),
        quote_amount=args.get("quote_amount"), eta_h=args.get("eta_h"),
        note=str(args.get("note") or ""),
        position_text=str(args.get("position_text") or ""), commit=False)


# 白名单：**只列这三个**。多列一个动作，承运商 agent 就多一份它不该有的权力。
INVOKE = {
    "accept_order": _do_accept,
    "submit_bid": _do_bid,
}


def _on_invoke(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """人（承运商自己的员工）委托一个写操作。

    失败必须**回话**，不能抛异常：抛异常会被 daemon 当成"处理失败"重投，
    而调用方还在 HTTP 上等着——最后等到 504，用户看到的是"agent 没响应"，
    真实原因是"这单已经被接走了"。业务拒绝与没人接活，是两件事。
    """
    p = msg.payload or {}
    action = str(p.get("action") or "")
    args = dict(p.get("args") or {})
    if handoff.stale(msg):
        # 迟到的执行比不执行更危险：人早已看到 504 并可能换了个做法
        bus.reply(db, msg, "reject",
                  {"reason": "这条委托已过期（请求方早已超时），没有执行"})
        return {"action": "stale", "asked": action}
    fn = INVOKE.get(action)
    if fn is None:
        bus.reply(db, msg, "reject",
                  {"reason": f"承运商 agent 不接受动作「{action}」"})
        return {"action": "rejected", "reason": "unknown_action", "asked": action}

    try:
        r = fn(db, msg, args)
    except (ValueError, tender_ops.TenderError) as exc:
        db.rollback()
        bus.reply(db, msg, "reject", {"reason": str(exc)[:400]})
        return {"action": "rejected", "asked": action, "reason": str(exc)[:120]}

    bus.reply(db, msg, "accept",
              {"event": f"承运商已完成「{action}」", "result": r})
    return {"action": action, "ok": True}


HANDLERS = {
    "counter": _on_negotiation,
    "accept": _on_negotiation,
    "reject": _on_negotiation,
    "request": _on_request,
    "notify": lambda db, m: {"action": "noted"},
    "invoke": _on_invoke,
}


def keys(db: Session) -> List[str]:
    """所有登记过的承运商组织 + 历史上收过消息的组织。

    只查档案会漏掉"没有档案但被投过消息"的 org（比如测试造的），
    那些消息会永远 pending。两个来源取并集。
    """
    orgs = {int(x) for x in db.execute(
        select(CarrierProfile.org_id)).scalars().all() if x}
    orgs |= {_org_of(x) for x in db.execute(
        select(AgentActor.agent_key).where(
            AgentActor.kind == KIND)).scalars().all()} - {None}
    return [key_for(o) for o in sorted(orgs)]


# ---------------------------------------------------------------------------
# 主动投标（默认关闭）
# ---------------------------------------------------------------------------
def _autobid_enabled() -> bool:
    return os.environ.get("SCA_CARRIER_AUTOBID", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _autobid(db: Session, key: str, limit: int = 5) -> List[Dict[str, Any]]:
    """看到还在收标的单就自己算价投标——"闲置运力主动报价"。

    默认关闭，原因写在 ``.env.example``：一开就会跟 smoke 脚本抢单，
    而且"每个承运商看到单就报"会把竞价变成刷单。真实业务里这一步该由
    "有没有顺路车"决定，那需要真实运力数据（见 README 能力边界）。
    """
    org = _org_of(key)
    if not org:
        return []
    prof = db.execute(select(CarrierProfile).where(
        CarrierProfile.org_id == org)).scalars().first()
    if not prof:
        return []
    rows = db.execute(select(Tender).where(Tender.status == "open").order_by(
        Tender.id.asc()).limit(limit)).scalars().all()
    out: List[Dict[str, Any]] = []
    for t in rows:
        exists = db.execute(select(CarrierBid.id).where(
            CarrierBid.order_id == t.order_id,
            CarrierBid.carrier_org_id == org)).scalar()
        if exists:
            continue
        try:
            r = tender_ops.submit_bid(
                db, {"user_id": prof.carrier_user_id, "org_id": org,
                     "role": "carrier"},
                t.order_id, commit=False)
            out.append({"order_id": t.order_id, "amount": r.get("quote"),
                        "bid_id": r.get("bid_id")})
            audit.log(db, {"user_id": prof.carrier_user_id or 0,
                           "account": "carrier:agent", "role": "carrier"},
                      "tender.autobid", target_type="order", target_id=t.order_id,
                      detail={"tender_id": t.id}, agent_key=key)
        except Exception as exc:                    # noqa: BLE001 弃标是正常结果
            out.append({"order_id": t.order_id, "skipped": str(exc)[:80]})
    return out


def due(db: Session, keys_: List[str]) -> Dict[str, Any]:
    if not _autobid_enabled():
        return {}
    out: Dict[str, Any] = {}
    for k in keys_:
        r = _autobid(db, k)
        if r:
            out[k] = r
    return {"autobid": out} if out else {}


class Daemon(AgentDaemon):
    kind = KIND
    poll_ms = 400
    due_interval_s = 20.0

    def keys(self, db: Session) -> List[str]:
        ks = keys(db)
        for k in ks:
            org = _org_of(k)
            if org:
                bus.register(db, k, KIND, org_id=org, utility=UTILITY)
        db.commit()
        return ks

    def handlers(self):
        return HANDLERS

    def due(self, db: Session, keys_):
        return due(db, keys_)


__all__ = ["Daemon", "KIND", "AGENT_KEY", "UTILITY", "HANDLERS", "INVOKE",
           "keys", "due", "key_for"]
