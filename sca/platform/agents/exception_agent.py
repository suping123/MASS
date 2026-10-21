"""异常域 Agent：只做"这是什么、多严重、该怎么处置"，不做商业决策。

为什么单独拆一个 Agent
----------------------
定级需要的信息是「异常描述 + 货类 + 吨位 + SOP」，处置决策需要的是「成本 + 报价 +
承运商状态」。放在同一个 Agent 里，商业信息会污染定级——比如这单利润很薄，就可能
被"值得不值得折腾"影响。拆开之后，这个 Agent 的输入视图里**根本没有** budget、
quote、cost 字段（见 `views.ROLE_FIELDS["exception"]`），这是可断言的边界。

它给出的 `options` 只是候选，不执行任何写操作；执行由 DispatchAgent 汇总后
经人审闸口完成。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..services import triage
from . import bus, contract, views

AGENT_KEY = "exception"
KIND = "exception"

# 目标权重：异常域只关心"别漏判、别误判"，不关心成本
UTILITY = {"safety": 0.5, "sla": 0.3, "cost": 0.0, "risk": 0.2}

# 这些故障意味着"车/货已经不能继续跑"，改派是唯一出路
_TOTAL_FAILURE = ("E-COLD-FAIL", "E-ACCIDENT", "E-DG-LEAK", "E-WX-TYPHOON")
# 这些故障改路线解决不了，只能改承诺时效或改派
_DEADLINE_HIT = ("E-SLA-DELAY", "E-JAM", "E-TMS-STOP", "E-WX-SNOW")


def _options(tri: Dict[str, Any], order) -> List[Dict[str, Any]]:
    """候选处置动作。每项都标"是否需要人审"，交给调度决策。"""
    code = tri.get("code") or ""
    lvl = tri.get("level") or "P2"
    human = bool(tri.get("approval"))

    opts: List[Dict[str, Any]] = [{
        "action": "resume", "label": "恢复在途",
        "why": "异常已排除，按原计划继续",
        "auto": not human, "approval": human,
    }]
    if code in _TOTAL_FAILURE:
        opts.append({
            "action": "reassign", "label": "改派承运商",
            "why": f"{tri.get('name')} 已导致运力不可用，改路线解决不了",
            "auto": False, "approval": True,
        })
    else:
        opts.append({
            "action": "replan", "label": "改道绕行",
            "why": "绕开受影响路段，保留原承运商与原车",
            "auto": not human, "approval": human,
        })
    if code in _DEADLINE_HIT or lvl in ("P0", "P1"):
        opts.append({
            "action": "extend", "label": "延长时效",
            "why": "路线不变但时限要重承诺，需通知货主",
            "auto": False, "approval": True,
        })
    opts.append({
        "action": "deliver", "label": "标记送达",
        "why": "货已交付，异常收口",
        "auto": False, "approval": True,
    })
    return opts


class ExceptionAgent:
    AGENT_KEY = AGENT_KEY

    def ensure(self, db: Session) -> None:
        bus.register(db, AGENT_KEY, KIND, utility=UTILITY)

    def triage(self, db: Session, order_id: int, description: str,
               *, exception_id: Optional[int] = None) -> Dict[str, Any]:
        """定级 + SOP + 候选动作。只读，不改任何状态。"""
        order = views.load_order(db, order_id, "exception")
        info = triage.suggest(description or "", order)
        tri = info["triage"]
        opts = _options(tri, order)
        self.ensure(db)
        # 记住最近一次同类判断，供"这条线以前怎么处理的"回溯（私有记忆）
        if tri.get("code"):
            bus.remember(db, AGENT_KEY, f"last:{tri['code']}",
                         {"level": tri["level"], "order_id": order_id,
                          "sla_min": tri.get("sla_min")})
        return {"triage": tri, "sop": info["sop"], "options": opts,
                "thread": contract.thread_of(order_id=order_id,
                                             exception_id=exception_id)}

    def propose(self, db: Session, order_id: int, description: str,
                *, exception_id: Optional[int] = None,
                to_agent: str = "dispatch") -> Dict[str, Any]:
        """把定级结论作为 `propose` 发给调度，供其决策。"""
        r = self.triage(db, order_id, description, exception_id=exception_id)
        tri = r["triage"]
        if not tri.get("code"):
            # 没有异常线索就不进处置链——不硬凑流程
            return {"sent": False, "reason": tri.get("note") or "无异常线索",
                    **r}
        msg = bus.send(db, from_agent=AGENT_KEY, to_agent=to_agent,
                       msg_type="propose",
                       payload={"amount": None,
                                "reason": f"{tri['name']}（{tri['level']}）"
                                          f"{tri.get('sla_min')} 分钟内处置",
                                "triage": tri,
                                "options": [o["action"] for o in r["options"]]},
                       order_id=order_id, exception_id=exception_id)
        return {"sent": True, "message_id": msg.id, **r}

    def replay(self, db: Session, order_id: int,
               limit: int = 5) -> List[Dict[str, Any]]:
        """同类异常的历史处置（来自私有记忆，不查别人的数据）。"""
        rows = bus.memories(db, AGENT_KEY, limit=50)
        return [r for r in rows if str(r.get("key", "")).startswith("last:")][:limit]


agent = ExceptionAgent()

__all__ = ["ExceptionAgent", "agent", "AGENT_KEY", "UTILITY"]
