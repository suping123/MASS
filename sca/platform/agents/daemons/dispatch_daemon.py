"""调度 Agent：开标、裁决、还价、流标处置。

它的三个局部目标（写进 ``UTILITY``，也真的进决策函数）：
**平台抽成 / 履约率 / 不流标**。所以它的行为跟另外三个都不同：
- 它不会为了压价把单子谈崩（流标是它最差的结局）；
- 但它**没有加价权**——超出可付上限就是超了，只能把缺口交给人；
- 到期不等人叫：`due()` 里扫"收标已截止"和"协商已超时"，自己往前推。

消息处理
--------
========  ==================================================
propose   异常域定级结论 → 选处置动作；人审项只发通知不执行
counter   承运商回价（带 tender_id）→ 进协商；不带 → 确定价拒绝
accept    承运商接受还价 → 定标
reject    承运商退出协商 → 判流标 + 升级货主
request   跨域取数，双向白名单校验
========  ==================================================
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AgentMessage, ExceptionReport, ShipperOrder, SysUser, Tender
from ...services import audit, notify as notify_svc, scheduling, tender_ops
from .. import bus, contract, handoff, negotiation, views
from ..daemon import AgentDaemon

KIND = "dispatch"
AGENT_KEY = "dispatch"

# 平台方目标：抽成要稳、单子要成、别出事。与货主（省钱）和承运商（多赚）都不同。
UTILITY = {"margin": 0.35, "fulfilment": 0.4, "risk": 0.25}

_ACTION_LABEL = {
    "resume": "恢复在途", "replan": "改道绕行", "extend": "延长时效",
    "deliver": "标记送达", "reassign": "改派承运商",
}
# 这些故障意味着车/货已经不能继续跑，改道解决不了
_TOTAL_FAILURE_CODES = ("E-COLD-FAIL", "E-ACCIDENT", "E-DG-LEAK", "E-WX-TYPHOON")


def _order_of(db: Session, msg: AgentMessage) -> Optional[int]:
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
    if tid.startswith("exc:"):
        try:
            e = db.get(ExceptionReport, int(tid.split(":", 1)[1]))
        except (TypeError, ValueError):
            return None
        return e.order_id if e else None
    return None


def pick_action(tri: Dict[str, Any], opts: List[str]) -> str:
    """调度选动作的规则（确定性，可读可测，不交给模型）：

    1. 车辆/货物本身不可用 → 只有改派能解决；
    2. 否则优先改道——保住原承运商与原车，重新找车的代价最大；
    3. 改道不可行再看能否只改承诺时效；
    4. 都不需要就是恢复在途。
    """
    code = tri.get("code") or ""
    if "reassign" in opts and code in _TOTAL_FAILURE_CODES:
        return "reassign"
    if "replan" in opts:
        return "replan"
    if "extend" in opts:
        return "extend"
    if "resume" in opts:
        return "resume"
    return opts[0] if opts else "resume"


def _notify_dispatchers(db: Session, title: str, body: str,
                        order_id: Optional[int] = None) -> int:
    """升级人工：给所有启用中的调度员各推一条站内通知。"""
    rows = db.execute(select(SysUser).where(
        SysUser.role == "dispatcher", SysUser.status == 1)).scalars().all()
    n = 0
    for u in rows:
        if notify_svc.push(db, u.id, ntype="exception", title=title,
                           body=body, order_id=order_id):
            n += 1
    return n


# ---------------------------------------------------------------------------
# 异常域 propose -> 处置
# ---------------------------------------------------------------------------
def _on_propose(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    p = msg.payload or {}
    tri = p.get("triage") or {}
    opts = [str(x) for x in (p.get("options") or [])]
    oid = _order_of(db, msg)
    pick = pick_action(tri, opts)
    label = _ACTION_LABEL.get(pick, pick)

    # 私有记忆：这单我选了什么、依据是哪条故障谱。异常域看不到这条记录。
    bus.remember(db, AGENT_KEY, f"decision:{msg.thread_id}",
                 {"pick": pick, "level": tri.get("level"),
                  "code": tri.get("code"), "order_id": oid})

    if contract.requires_human(pick):
        n = _notify_dispatchers(
            db, f"订单异常待处置：{label}",
            f"{tri.get('name') or '异常'}（{tri.get('level') or '-'}）"
            f"建议处置为「{label}」。该动作需要人工确认，系统不自动执行。",
            order_id=oid)
        bus.reply(db, msg, "notify",
                  {"event": f"已升级人工审批：{label}",
                   "reason": f"命中 {tri.get('level') or '-'} {tri.get('name') or ''}，"
                             f"该动作属人审项（已通知 {n} 名调度）"})
        audit.log(db, {"user_id": 0, "account": "dispatch:agent", "role": "dispatcher"},
                  "exception.escalate", target_type="order", target_id=oid,
                  reason=label, detail={"pick": pick, "notified": n},
                  agent_key=AGENT_KEY)
        return {"action": "escalated", "pick": pick, "notified": n}

    bus.reply(db, msg, "accept", {"note": f"调度接受处置建议：{label}"})
    return {"action": "accepted", "pick": pick}


# ---------------------------------------------------------------------------
# 协商
# ---------------------------------------------------------------------------
def _on_counter(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """承运商的 counter / accept / reject。

    带 ``tender_id`` 的是招标协商（真的在谈价）；不带的按 v5 语义处理：
    挂牌价是确定价，如实拒绝——不能因为系统里有了"还价"这个词，
    就把所有 counter 都当成谈判邀请。
    """
    p = msg.payload or {}
    if p.get("tender_id"):
        return negotiation.dispatch_handle_counter(db, msg)

    if msg.msg_type == "counter":
        bus.reply(db, msg, "reject",
                  {"reason": "挂牌价为确定价，不接受议价；如需调整需由调度走改单流程"})
        return {"action": "rejected", "reason": "no_tender"}
    return {"action": "ignored"}


# ---------------------------------------------------------------------------
# 跨域取数
# ---------------------------------------------------------------------------
def _on_request(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    p = msg.payload or {}
    fields = [str(f) for f in (p.get("fields") or [])]
    # 双向校验：我有没有这个字段 **且** 请求方看不看得见。
    # 只查"我有没有"会漏——调度自己看得到 budget_max，于是会把它答给承运商，
    # 信息边界就在这条路径上被绕开了。
    mine = views.visible_fields("dispatch")
    theirs = views.visible_fields(contract.kind_of(msg.from_agent))
    bad = [f for f in fields if f not in mine or f not in theirs]
    if bad:
        bus.reply(db, msg, "reject",
                  {"reason": f"越权取数：{', '.join(bad)} 不在可取范围内"})
        return {"action": "rejected", "fields": bad}

    oid = _order_of(db, msg)
    if not oid:
        bus.reply(db, msg, "reject", {"reason": "消息没带订单号，无法取数"})
        return {"action": "rejected", "reason": "no_order"}
    o = views.load_order(db, oid, "dispatch")
    if o is None:
        bus.reply(db, msg, "reject", {"reason": f"订单 {oid} 不存在"})
        return {"action": "rejected", "reason": "no_such_order"}
    d = o.as_dict()
    bus.reply(db, msg, "notify",
              {"event": "字段返回", "fields": {k: str(d.get(k)) for k in fields}})
    return {"action": "answered", "fields": fields}


def _on_notify(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    return {"action": "noted"}


# ---------------------------------------------------------------------------
# 人类委托（S5）：定价挂牌
# ---------------------------------------------------------------------------
def _do_publish(db: Session, msg: AgentMessage, args: Dict[str, Any]) -> Dict[str, Any]:
    u = dict(args.get("user") or {})
    user = {"user_id": int(u.get("user_id") or 0), "role": "dispatcher",
            "org_id": u.get("org_id"), "account": str(u.get("account") or "dispatch")}
    oid = int(args.get("order_id") or 0)
    # 写路径要真实实例（挂牌要改状态、要发事件），视图是只读的
    order = db.get(ShipperOrder, oid)          # noqa: isolation
    if order is None:
        raise ValueError(f"订单 {oid} 不存在")
    gross = args.get("gross")
    return scheduling.publish_order(
        db, order, user, gross=(float(gross) if gross not in (None, "") else None))


# 挂牌是"调度把单子推到池子里"，是成交链的起点。方案生成不在此列：
# 出方案是调度自己的准备动作（不涉及他方、不改归属），留在请求内更省事。
INVOKE = {"publish_order": _do_publish}


def _on_invoke(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """人（调度员）委托调度 agent 执行一个写操作。

    为什么"调度员点挂牌"也要走消息：挂牌之后承运商才能投标，这是成交链的第一环。
    如果它在 HTTP 请求内直接执行，关掉调度 agent 依然能挂牌——那 dispatch 进程
    就只是个盯着收件箱的空转程序，第 5 条检验立刻不成立。
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
        bus.reply(db, msg, "reject", {"reason": f"调度 agent 不接受动作「{action}」"})
        return {"action": "rejected", "reason": "unknown_action", "asked": action}

    try:
        r = fn(db, msg, args)
    except (ValueError, tender_ops.TenderError) as exc:
        db.rollback()
        bus.reply(db, msg, "reject", {"reason": str(exc)[:400]})
        return {"action": "rejected", "asked": action, "reason": str(exc)[:120]}

    bus.reply(db, msg, "accept",
              {"event": f"调度已完成「{action}」", "result": r})
    return {"action": action, "ok": True}


HANDLERS = {
    "propose": _on_propose,
    "counter": _on_counter,
    "accept": _on_counter,
    "reject": _on_counter,
    "request": _on_request,
    "notify": _on_notify,
    "invoke": _on_invoke,
}


def keys(db: Session) -> List[str]:
    return [AGENT_KEY]


# ---------------------------------------------------------------------------
# 到期检查：不等人叫
# ---------------------------------------------------------------------------
def due(db: Session, keys_: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # 1) 收标截止 → 裁决（可能定标 / 进协商 / 延期 / 流标）
    closed = tender_ops.sweep_due(db)
    if closed:
        out["closed"] = len(closed)
        # 2) 刚进入协商的，立刻发第一轮还价。不发的话它会停在那儿，
        #    直到超时被判死——「到期有动作」必须闭环，不能只改状态。
        started = 0
        for r in closed:
            if r.get("status") != "negotiating":
                continue
            t = db.get(Tender, int(r.get("tender_id") or 0))
            if t is None:
                continue
            negotiation.start_negotiation(db, t, commit=False)
            started += 1
        if started:
            out["countered"] = started

    # 3) 协商超时 → 判死 + 升级货主
    expired = negotiation.sweep_timeouts(db)
    if expired:
        out["expired"] = len(expired)
    return out


class Daemon(AgentDaemon):
    kind = KIND
    poll_ms = 300                  # 调度是链路中枢，等短一点
    due_interval_s = 15.0

    def keys(self, db: Session) -> List[str]:
        bus.register(db, AGENT_KEY, KIND, utility=UTILITY)
        db.commit()
        return keys(db)

    def handlers(self):
        return HANDLERS

    def due(self, db: Session, keys_):
        return due(db, keys_)


__all__ = ["Daemon", "KIND", "AGENT_KEY", "UTILITY", "HANDLERS", "INVOKE",
           "keys", "due", "pick_action"]
