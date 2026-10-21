"""货主 Agent：只回答"这笔钱我认不认"，不替人签字。

它能做的事很少，这是刻意的：
- 收到 ``escalate``（承运商要价高于可付上限）→ 把缺口翻成人话告诉货主本人，
  并回一条 ``notify`` 说明"建议已收到，等本人确认"；
- 它**不能**改 ``budget_max``。改预算等于改合同，属
  ``contract.HUMAN_REQUIRED.price_increase``，必须人点。

为什么不让货主 Agent 自动加价
----------------------------
"加到多少还能接受"是可以算的（预算上浮上限 + 缺口），但算出来的是"平台建议"，
不是"货主同意"。Agent 自己点头，多 Agent 就退化成了"平台替双方定价"，
跟改造前那张单边定价表没有区别。所以这里的终止条件写死：
**说明缺口 → 交人**，没有第二条路。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import AgentMessage, ShipperOrder
from ...services import audit, notify, orders
from ...services.orders import add_event
from .. import bus, handoff, views
from ..daemon import AgentDaemon

KIND = "shipper"
AGENT_KEY = "shipper"

# 局部目标：总成本最低，其次按时，最后服务体验。与承运商（利润）天然冲突。
UTILITY = {"cost": 0.5, "on_time": 0.3, "service": 0.2}


def _order_id(db: Session, msg: AgentMessage) -> Optional[int]:
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


def _on_escalate(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """调度把"谈不拢"交给货主。货主只做两件事：说明、交人。"""
    p = msg.payload or {}
    oid = _order_id(db, msg)
    if not oid:
        return None
    order = views.load_order(db, oid, KIND)
    if order is None:
        return None

    gap = float(p.get("gap") or 0)
    reason = str(p.get("reason") or "协商无果")
    raw = db.get(ShipperOrder, oid)          # noqa: isolation —— 写路径需要真实实例
    budget = float(raw.budget_max or 0)

    # 私有记忆：货主记得"这单我原本给多少、对方要多少"。
    # 这条记录承运商与调度都读不到（按 agent_key 隔离）。
    bus.remember(db, AGENT_KEY, f"escalate:{oid}",
                 {"budget_max": budget, "gap": gap, "reason": reason,
                  "decision": "pending_human", "ask": p.get("ask")})

    notify.push(db, raw.shipper_user_id, ntype="negotiation",
                title=f"订单 {raw.order_no} 需要你决定：是否提高预算",
                body=(f"承运商最低可接受价高出可付上限 ¥{gap:.2f}。" if gap else "")
                     + "提价属于人工决定，系统不会自动改。",
                order_id=oid)
    add_event(db, oid, "shipper_escalation_seen", "shipper",
              raw.shipper_user_id,
              {"gap": gap, "tender_id": p.get("tender_id"), "msg_id": msg.id})
    audit.log(db, {"user_id": raw.shipper_user_id or 0, "account": "shipper:agent",
                   "role": "shipper"},
              "shipper.escalation_ack", target_type="order", target_id=oid,
              reason=reason, detail={"gap": gap}, agent_key=AGENT_KEY)

    bus.reply(db, msg, "notify",
              {"event": f"货主已知悉：缺口 ¥{gap:.2f}，待本人确认是否提价",
               "gap": gap, "order_id": oid})
    return {"action": "await_human", "gap": gap, "order_id": oid}


# ---------------------------------------------------------------------------
# 人类委托（S5）：落单
# ---------------------------------------------------------------------------
def _do_create(db: Session, msg: AgentMessage, args: Dict[str, Any]) -> Dict[str, Any]:
    u = dict(args.get("user") or {})
    user = {"user_id": int(u.get("user_id") or 0), "org_id": u.get("org_id"),
            "role": "shipper", "account": str(u.get("account") or "shipper")}
    slots = dict(args.get("slots") or {})

    # 幂等：同一次对话确认只落一张单。等待超时后用户再说一次「确认」很常见，
    # 没有这道门就会多出一张一模一样的单——而那张单没人知道该取消哪张。
    key = f"create:{args.get('session_id') or ''}"
    if args.get("session_id"):
        prev = bus.recall(db, AGENT_KEY, key)
        if isinstance(prev, dict) and prev.get("order_id"):
            return {**prev, "deduped": True}

    r = orders.create_order(db, user, slots)
    if args.get("session_id"):
        bus.remember(db, AGENT_KEY, key, {"order_id": r["order_id"],
                                          "order_no": r["order_no"]})
    return r


INVOKE = {"create_order": _do_create}


def _on_invoke(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """人（货主本人）委托货主 agent 落单。

    对话状态机（解析槽位 → 复述 → 等确认）仍然在 HTTP 请求里跑，那是"人在跟
    agent 说话"，不是委托。但**落单这个写操作**交给常驻进程：它要写订单、写事件、
    推状态，和后面挂牌/投标/接单是同一条链，必须能一起被"移除交互"测出来。
    """
    p = msg.payload or {}
    action = str(p.get("action") or "")
    args = dict(p.get("args") or {})
    if handoff.stale(msg):
        bus.reply(db, msg, "reject",
                  {"reason": "这条委托已过期（请求方早已超时），没有执行"})
        return {"action": "stale", "asked": action}
    fn = INVOKE.get(action)
    if fn is None:
        bus.reply(db, msg, "reject", {"reason": f"货主 agent 不接受动作「{action}」"})
        return {"action": "rejected", "reason": "unknown_action", "asked": action}

    try:
        r = fn(db, msg, args)
    except ValueError as exc:
        db.rollback()
        bus.reply(db, msg, "reject", {"reason": str(exc)[:400]})
        return {"action": "rejected", "asked": action, "reason": str(exc)[:120]}

    bus.reply(db, msg, "accept",
              {"event": f"货主 agent 已完成「{action}」", "result": r})
    return {"action": action, "ok": True}


HANDLERS = {
    "escalate": _on_escalate,
    "notify": lambda db, m: {"action": "noted"},
    "invoke": _on_invoke,
}


def keys(db: Session) -> List[str]:
    return [AGENT_KEY]


def due(db: Session, keys_: List[str]) -> Dict[str, Any]:
    """货主侧没有"到点必须做的事"——不下单就没有业务，不该凭空发起。"""
    return {}


class Daemon(AgentDaemon):
    kind = KIND
    poll_ms = 800                 # 人等他，不着急；800ms 足够"点了就看到"

    def keys(self, db: Session) -> List[str]:
        bus.register(db, AGENT_KEY, KIND, utility=UTILITY)
        db.commit()
        return keys(db)

    def handlers(self):
        return HANDLERS

    def due(self, db: Session, keys_):
        return due(db, keys_)


__all__ = ["Daemon", "KIND", "AGENT_KEY", "UTILITY", "HANDLERS", "keys", "due"]
