"""Evaluator —— 验收 worker 产出（deterministic-first）。

**为什么不以模型为主**：本系统没有地面真值——承运商是内置样本池、在途没有真实 GPS/IoT
（`REF-01` 已逐条核实）。让模型去"复核"里程或成本，只会用另一个幻觉检查幻觉。所以这里
只做**可判定的事实校验**：产出在不在、口径自不自洽、降级有没有如实标注。模型的语义复核
将来只作为补充（且必须 fail-open），不承担判定责任。

校验结论（verdict）直接进 `evaluation`，并成为 supervisor 后续动作的前提条件：
  * `accept` —— 无缺陷，`accepted[worker]=True`；
  * `retry`  —— 有缺陷但还有重试机会（`MAX_RETRY`），对应 worker **重新开放**
                可执行性，supervisor 会重派它一次；
  * `reject` —— 重试机会用尽，进 `needs_human`，`execute` 被硬挡、由 `approval_gate`
                的人工闸口承接。

也就是说 **"做完对不对"是真的在拦人**（下发被挡），而且**不会死锁**——这一点比
"判定得更准"更要紧：一个永远不 accept 的校验器会让整单静默停住。

**校验器必须"忽略合法的降级"**：起讫缺失、同城、外部服务不可用都会产出降级结果，
它们如实标注 `degraded/degraded_reason` 后是**合法产出**，不是缺陷。把降级当缺陷，
正常单会被反复重试到人工升级——这是最容易被写错的地方，所以下面每个校验器都先判
"是否属于合法降级"，再判"是否自相矛盾"。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .precondition import MAX_RETRY

# worker -> 它独占写的 state 分区（用于推断"刚完成的是谁"）
PARTITIONS: Dict[str, str] = {
    "plan_prepare": "plan_context",
    "agent_route": "route",
    "agent_precheck": "precheck",
    "plan_arbiter": "current_plan",
}

# 校验器返回"缺陷列表"；空列表 = 通过
Checks = Dict[str, List[str]]


def _ctx(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("plan_context") or {}


def _has_endpoints(s: Dict[str, Any]) -> bool:
    c = _ctx(s)
    return bool(c.get("origin") and c.get("dest"))


def _check_route(s: Dict[str, Any]) -> List[str]:
    route = s.get("route") or {}
    if not route:
        return ["route 分区为空"]
    defects: List[str] = []
    # 降级（map-mcp 不可用 / 同城 / 起讫无法解析）如实标注后是合法产出：
    # 只要求"能追溯"，不要求里程为正。
    degraded = bool(route.get("degraded"))
    if degraded:
        if not route.get("degraded_reason"):
            defects.append("标注了 degraded 却未给出 degraded_reason（无法追溯降级原因）")
        return defects
    if _has_endpoints(s):
        if not route.get("waypoints"):
            defects.append("有起讫、未降级，但 route.waypoints 为空")
        if not (float(route.get("distance_km") or 0) > 0):
            defects.append("有起讫、未降级，但 route.distance_km 非正")
    return defects


def _check_precheck(s: Dict[str, Any]) -> List[str]:
    pre = s.get("precheck") or {}
    if not pre:
        return ["precheck 分区为空"]
    defects: List[str] = []
    oc = pre.get("order_check")
    if not isinstance(oc, dict) or "exists" not in oc:
        defects.append("order_check 缺少 exists 结论（订单核验未真正执行）")
    # degraded 的合法形态：标注 + 给出 notes 说明（如"订单核验不可用"）
    if pre.get("degraded") and not (pre.get("notes") or []):
        defects.append("标注了 degraded 却没有任何 notes（降级不可追溯）")
    return defects


def _check_plan(s: Dict[str, Any]) -> List[str]:
    plan = s.get("current_plan") or {}
    if not plan:
        return ["current_plan 为空（仲裁未产出方案）"]

    defects: List[str] = []
    # ① "仲裁真的跑过吗"：依据链缺失 = 没真算，而不是"算出来没选到车"
    if not (s.get("plan_arbitration") or {}):
        defects.append("plan_arbitration 缺失（仲裁依据链没有落痕）")
    if "carriers" not in plan:
        defects.append("current_plan 缺少 carriers 字段（运力匹配未执行）")

    # ② 降级（起讫缺失/同城/地图不可用）合法：只要求能追溯
    if plan.get("degraded"):
        if not plan.get("degraded_reason"):
            defects.append("标注 degraded 却未给 degraded_reason（降级不可追溯）")
        return defects

    # ③ 未降级：口径必须自洽。**不要求有承运商** —— "匹配不到运力，挂起待补"
    #    是真实业务结论（execute 会返回 pending_carrier），不是规划缺陷。
    if _has_endpoints(s):
        if not plan.get("waypoints"):
            defects.append("有起讫、未降级，但 current_plan 没有途经点")
        if not (float(plan.get("distance_km") or 0) > 0):
            defects.append("有起讫、未降级，但 current_plan.distance_km 非正")
    return defects


CHECKS: Dict[str, Any] = {
    "agent_route": _check_route,
    "agent_precheck": _check_precheck,
    "plan_arbiter": _check_plan,
}


def evaluate(worker: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """验收单个 worker 的产出（**纯函数**：只读 state，不写、不落盘、不发外部请求）。

    `attempts` 是本次评估后该 worker 的累计被评估次数，由 `state.evaluation.attempts`
    单调递增得来。节点若在超步 replay 时被重跑，输入 state 相同 -> 输出相同，因此这个
    计数是幂等的（LangGraph 的超步写是全量提交，不会留下半成品）。
    """
    check = CHECKS.get(worker)
    ev = state.get("evaluation") or {}
    attempts = int((ev.get("attempts") or {}).get(worker, 0)) + 1

    if check is None:
        return {"worker": worker, "verdict": "accept", "defects": [], "attempts": attempts,
                "note": "该 worker 无专属校验器（产出口径由下游 precondition 兜底）"}

    defects = check(state)
    if not defects:
        verdict = "accept"
    elif attempts <= MAX_RETRY:
        verdict = "retry"
    else:
        verdict = "reject"
    return {"worker": worker, "verdict": verdict, "defects": defects, "attempts": attempts}


def pending_workers(state: Dict[str, Any]) -> List[str]:
    """推断"已产出但尚未验收通过"的 worker。

    为什么靠推断而不是靠参数：这是**扇入汇聚点**，`plan_prepare` / `agent_route` /
    `agent_precheck` / `plan_arbiter` 都指向它（同超步的并行支会各自到达），节点签名里
    没有"我从哪来"。按"分区已填充 + 尚未 accept"推断，天然幂等：已 accept 的不会重复
    计入，重试后的新产出（accept 仍为空）会被重新纳入。
    """
    accepted = (state.get("evaluation") or {}).get("accepted") or {}
    out: List[str] = []
    for worker, key in PARTITIONS.items():
        if state.get(key) and not accepted.get(worker):
            out.append(worker)
    return out
