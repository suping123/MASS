"""异常域 Agent：只判断"多严重、该怎么处置"，不看钱。

它收到的每一条消息都来自承运商（``propose`` 报异常）。处理链：

    承运商 propos e  ->  定级（故障谱 + SOP）  ->  propose 给调度  ->  回执给承运商

定级的输入是「异常描述 + 货类 + 吨位 + SOP」，`views.ROLE_FIELDS["exception"]`
里根本没有 budget / quote / cost —— 这一条是可断言的边界，不是自觉。

它**不执行任何写操作**：改道、改期、改派都是调度的事，且都要过人审闸口。
它给出的只有候选动作和"要不要人审"的标记。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import AgentMessage, ExceptionReport
from ...services import audit, triage
from .. import bus, contract
from ..daemon import AgentDaemon
from ..exception_agent import agent as exception_agent

KIND = "exception"
AGENT_KEY = "exception"

# 目标权重：安全与 SLA 优先，成本权重写死 0——不是"暂时没配"，是"不应该有"
UTILITY = {"safety": 0.5, "sla": 0.3, "cost": 0.0, "risk": 0.2}


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


def _exc_of(msg: AgentMessage) -> Optional[int]:
    tid = str(msg.thread_id or "")
    if tid.startswith("exc:"):
        try:
            return int(tid.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def _on_report(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    """承运商报异常 → 定级 → 转调度。"""
    p = msg.payload or {}
    if msg.msg_type not in ("propose", "request"):
        return {"action": "ignored"}

    oid = _order_of(db, msg)
    if not oid:
        bus.reply(db, msg, "reject",
                  {"reason": "消息没带可识别的订单（thread 无法解析），无法定级"})
        return {"action": "rejected", "reason": "no_order"}

    desc = str(p.get("description") or p.get("reason") or "").strip()
    if len(desc) < 2:
        bus.reply(db, msg, "reject", {"reason": "异常描述为空，无法定级"})
        return {"action": "rejected", "reason": "empty_desc"}

    r = exception_agent.propose(db, oid, desc, exception_id=_exc_of(msg),
                                to_agent="dispatch")
    tri = r.get("triage") or {}
    audit.log(db, {"user_id": 0, "account": "exception:agent", "role": "exception"},
              "exception.triage", target_type="order", target_id=oid,
              reason=str(tri.get("name") or ""),
              detail={"code": tri.get("code"), "level": tri.get("level"),
                      "msg_id": msg.id},
              agent_key=AGENT_KEY)

    if contract.kind_of(msg.from_agent) == "carrier":
        if r.get("sent"):
            bus.reply(db, msg, "notify",
                      {"event": f"异常已受理并定级 {tri.get('level')}"
                                f"（{tri.get('name')}），已转调度处置"})
        else:
            bus.reply(db, msg, "notify",
                      {"event": f"已记录异常，但未能匹配故障谱：{r.get('reason')}"})
    return {"action": "triaged", "code": tri.get("code"),
            "level": tri.get("level"), "forwarded": bool(r.get("sent")),
            "order_id": oid}


def _on_notify(db: Session, msg: AgentMessage) -> Optional[Dict[str, Any]]:
    return {"action": "noted"}


HANDLERS = {
    "propose": _on_report,
    "request": _on_report,
    "notify": _on_notify,
}


def keys(db: Session) -> List[str]:
    return [AGENT_KEY]


def due(db: Session, keys_: List[str]) -> Dict[str, Any]:
    """定时巡查在途天气。默认关闭：它会打真实天气 API，且在 smoke 里会凭空
    多出异常单。要用就设 ``SCA_WEATHER_WATCH=1``。"""
    import os

    if os.environ.get("SCA_WEATHER_WATCH", "0").strip().lower() not in ("1", "true", "on"):
        return {}
    from ...services import weather_watch

    r = weather_watch.scan_inflight(db, limit=3, dry_run=False)
    return {"weather": r}


class Daemon(AgentDaemon):
    kind = KIND
    poll_ms = 500

    def keys(self, db: Session) -> List[str]:
        bus.register(db, AGENT_KEY, KIND, utility=UTILITY)
        db.commit()
        return keys(db)

    def handlers(self):
        return HANDLERS

    def due(self, db: Session, keys_):
        return due(db, keys_)


__all__ = ["Daemon", "KIND", "AGENT_KEY", "UTILITY", "HANDLERS", "keys", "due"]
