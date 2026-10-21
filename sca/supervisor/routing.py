"""路由辅助：原拓扑复刻（影子模式对照基线）+ 决策留痕签名。

两个函数都**只读 state、只返回数据**，不写状态（与 `decide()` 同一条红线）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .precondition import needs_approval

# 规划段被 supervisor 接管的四个 worker（各自独占一个 state 分区）
WORKERS = ("plan_prepare", "agent_route", "agent_precheck", "plan_arbiter")


def legacy_route(state: Dict[str, Any]) -> List[str]:
    """**逐条复刻**改造前那张静态图在该 state 下的下一跳。

    影子模式（`SCA_SUPERVISOR_MODE=shadow`）用它来决定真实路由：决策照算、照记，
    但**路由仍按原拓扑走**。这样"先观察不生效"才成立——一旦用 `decide()` 的结果去
    跳转，影子模式就变成了 live，只是换了个名字。

    复刻口径（对照改造前的 `compile()`）：
      * `plan_context` 缺      -> `plan_prepare`
      * 两个分支任一缺          -> 缺哪个派哪个（原图是 `plan_prepare` 一次扇出两支，
                                  同超步并行，两条边等价于这一个列表）
      * `current_plan` 缺      -> `plan_arbiter`
      * P0/P1 且未审批          -> `approval_gate`（原图的 conditional_edges）
      * 尚未下发                -> `execute`
      * 其余                    -> `monitor`（原图 `execute -> monitor` 静态边）
    """
    if not state.get("plan_context"):
        return ["plan_prepare"]

    route, pre = state.get("route") or {}, state.get("precheck") or {}
    if not route or not pre:
        todo: List[str] = []
        if not route:
            todo.append("agent_route")
        if not pre:
            todo.append("agent_precheck")
        return todo

    if not state.get("current_plan"):
        return ["plan_arbiter"]

    if needs_approval(state) and not (state.get("approval") or {}).get("approved"):
        return ["approval_gate"]

    if not state.get("execution"):
        return ["execute"]
    return ["monitor"]


def decision_sig(state: Dict[str, Any]) -> str:
    """决策输入的稳定摘要 —— 只取 `decide()` / `legacy_route()` 真正读的字段是否就位。

    用途：`supervisor_trace` 是累加通道，而 fan-in 汇聚点在超步 replay 时可能被重跑，
    直接 append 会重复留痕（幂等键缺失）。这里给每条留痕打一个签名，节点写之前比对
    上一条：签名相同 = 输入完全没变 = 同一次决策被重放，跳过。
    """
    ev = state.get("evaluation") or {}
    summary = {
        "ctx": bool(state.get("plan_context")),
        "route": bool(state.get("route")),
        "precheck": bool(state.get("precheck")),
        "plan": bool(state.get("current_plan")),
        "approved": bool((state.get("approval") or {}).get("approved")),
        "esc_resolved": bool((state.get("approval") or {}).get("escalation_resolved")),
        "exec": bool(state.get("execution")),
        "flagged": sorted(ev.get("flagged") or {}),
        "needs_human": sorted(ev.get("needs_human") or {}),
    }
    blob = json.dumps(summary, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:10]
