"""Supervisor 化（S3 竖切片）：让"谁来做 / 什么时候做 / 做完对不对 / 下一步做什么"有唯一落点。

五个模块各管一件事，且**决策与执行分离**：
  * `precondition` —— ELIGIBLE 的唯一来源：用确定性代码算出"当前合法可执行集合"，
                      并把业务不变量（没审批不下发、校验未过不下发）写成硬判定。
  * `decide`       —— 纯函数决策：只能在 ELIGIBLE 内选目标，返回结构化依据。
  * `routing`      —— 原拓扑复刻（影子模式对照基线）+ 决策留痕签名。
  * `evaluate`     —— 验收 worker 产出：确定性校验优先、忽略合法降级（见模块 docstring）。
  * 图节点（`sca/agents/__init__.py` 的 `supervisor` / `plan_evaluator`）只做适配与落痕。

**为什么要拆这么细**：`decide()` 必须能被两个消费者复用——
  * live   适配器 -> `Command(goto=...)`，真的改变路由；
  * shadow 适配器 -> 路由仍走 `routing.legacy_route()`（原拓扑），只把决策与"是否一致"
                     记进 `supervisor_trace`。
影子模式（先观察不生效）是这套改造唯一能在真实流量上验证决策质量、又不承担行为风险的
手段；一旦把决策写死在 `Command` 构造里，影子模式就没法做了。

**当前范围（竖切片）**：只接管规划段（prepare / route / precheck / arbiter）+ 规划后的
审批-下发分岔（approval_gate / execute）。执行段及其后仍走原有静态边，等竖切片验证通过
再逐步扩展。
"""
from .decide import Decision, decide, validate_targets
from .evaluate import MAX_RETRY as WORKER_MAX_RETRY
from .evaluate import evaluate, pending_workers
from .precondition import (ACTIONS, ORDER, PARALLEL_PAIR, eligible, gate_passed,
                           handoff_target, needs_approval, stall_reasons)
from .routing import WORKERS, decision_sig, legacy_route

__all__ = [
    "Decision", "decide", "validate_targets",
    "evaluate", "pending_workers", "WORKER_MAX_RETRY",
    "ACTIONS", "ORDER", "PARALLEL_PAIR", "eligible", "handoff_target",
    "needs_approval", "gate_passed", "stall_reasons",
    "WORKERS", "decision_sig", "legacy_route",
]
