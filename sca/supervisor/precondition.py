"""前置条件表 —— ELIGIBLE（当前合法可执行集合）的**唯一定义**。

**为什么必须用确定性代码算集合**：让模型"决定谁来做"不等于让它自由编排。主干里有
业务不变量——没审批不能下发、没验收不能对外发消息——这些不能交给模型自觉。所以先把
"合法可执行的集合"用可读、可测、可复现的判定算出来，模型（将来启用时）只能在这个
集合里做优先级与并行组合，**不得新增目标**。

判定函数统一签名 `(state) -> (bool, str)`：第二个返回值是"依据"，会随决策一起落痕，
便于事后核对"它当时凭什么认为这一步可以做"。

**有界自主的第二道闸（本轮新增）**：校验未通过（`evaluation.flagged`）的产出会**重新
开放**对应 worker 的可执行性（重试一次）；再失败则进 `needs_human`，此时 `execute`
被硬性挡住、由 `approval_gate` 的人工闸口承接。这样"做完对不对"是真在拦人，且
**不会死锁**（死锁的写法是"不 accept 就永远不 eligible"，那会让整单静默停住）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# —— 动作名（与图节点名一一对应，拼错就是静默失效，所以集中定义）——
PLAN_PREPARE = "plan_prepare"
AGENT_ROUTE = "agent_route"
AGENT_PRECHECK = "agent_precheck"
PLAN_ARBITER = "plan_arbiter"
APPROVAL_GATE = "approval_gate"
EXECUTE = "execute"

# 规划段跑完后交回主干的落点：执行段保持原静态拓扑，不随本轮改造变动。
HANDOFF_MONITOR = "monitor"
HANDOFF_END = "end"
HANDOFF = (HANDOFF_MONITOR, HANDOFF_END)

# 单个 worker 的产出最多被重派几次。1 次：缺陷多为系统性口径错误，反复重跑只是烧时间；
# 第 2 次仍不合格 -> 转人工（`needs_human`），不无限自我修复。
MAX_RETRY = 1


def needs_approval(state: Dict[str, Any]) -> bool:
    """P0/P1 属高危，必须走人工审批闸口。

    单一来源：`sca/agents` 的 `_needs_approval` 直接转调本函数，避免两处判定漂移
    （条件边、审批节点、supervisor 三处都依赖这条规则）。
    """
    return (state.get("exception") or {}).get("level") in ("P0", "P1")


# —— state 分区读取（保持每个判定都是纯读）——
def _ctx(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("plan_context") or {}


def _route(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("route") or {}


def _precheck(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("precheck") or {}


def _plan(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("current_plan") or {}


def _ev(s: Dict[str, Any]) -> Dict[str, Any]:
    return s.get("evaluation") or {}


def _accepted(s: Dict[str, Any]) -> Dict[str, Any]:
    return _ev(s).get("accepted") or {}


def _flagged(s: Dict[str, Any]) -> Dict[str, Any]:
    """校验发现缺陷、尚有重试机会的 worker -> 缺陷列表。"""
    return _ev(s).get("flagged") or {}


def _needs_human(s: Dict[str, Any]) -> Dict[str, Any]:
    """重试机会已用尽、必须人工确认的 worker -> 缺陷列表。"""
    return _ev(s).get("needs_human") or {}


def _esc_answered(s: Dict[str, Any]) -> bool:
    """升级闸口是否已经问过人了。

    用它而不是"是否被批准"来判定"要不要再挂起"：否则驳回后 `approval_gate` 又会重新
    判为可执行 -> 再次 interrupt -> 调度员被反复弹同一个框（死循环）。
    "问过了 = 不再自动挂起"，若结果是驳回则 `_act_execute` 挡住下发，整单在 `end` 收口。
    """
    return bool((s.get("approval") or {}).get("escalation_resolved"))


def _has_endpoints(s: Dict[str, Any]) -> bool:
    c = _ctx(s)
    return bool(c.get("origin") and c.get("dest"))


def _route_ready(s: Dict[str, Any]) -> bool:
    """路线是否就绪。无起讫时**不要求**路线（节点本身会如实返回空路线，不编造）。"""
    if not _has_endpoints(s):
        return True
    return bool(_route(s).get("waypoints"))


def gate_passed(approval: Optional[Dict[str, Any]]) -> bool:
    """人工闸口是否放行 —— 「驳回不得下发」的唯一判定口径。

    放行只有两种合法形态：
      * `auto` 为真 —— 非高危、本来就不需要审批（`approval_gate` 自动通过）；
      * `approved` 为真 —— 调度员点了通过。
    其余一律视为**未放行**（含"没有审批记录"这种 fail-closed 情况）。

    为什么必须收成一处：这条判定有三个读者——`approval_gate` 的出边（放行才去
    `execute`）、`execute` 自身的守卫（最后一道防线）、supervisor 的 `_act_execute`
    （ELIGIBLE 表）。三处各写一套迟早漂移，而漂移的后果是"人工驳回仍被下发"。
    """
    apr = approval or {}
    if apr.get("auto"):
        return True
    return bool(apr.get("approved"))


def _approved(s: Dict[str, Any]) -> bool:
    return gate_passed(s.get("approval"))


def retry_pending(s: Dict[str, Any], worker: str) -> bool:
    """该 worker 的产出被标注缺陷、且还有重试机会 —— 应重新派发。

    注意"唯一写者"仍然成立：重派的是**同一个 worker**，它依旧是该分区的唯一写者；
    重试带来的是**重复写**，不是**并行写**（supervisor 一次只派一个目标，也不会把
    同一 worker 与其它分支放进同一批）。
    """
    if worker not in _flagged(s):
        return False
    return worker not in _needs_human(s)


# ---------------------------------------------------------------------------
# 各动作的前提判定
# ---------------------------------------------------------------------------
def _act_plan_prepare(s: Dict[str, Any]) -> Tuple[bool, str]:
    if _ctx(s):
        return False, "plan_context 已生成"
    return True, "规划上下文未生成"


def _act_agent_route(s: Dict[str, Any]) -> Tuple[bool, str]:
    if not _ctx(s):
        return False, "plan_context 未生成（起讫口径未就绪）"
    if not _route(s):
        if not _has_endpoints(s):
            return False, "起讫不完整：不调用地图，也不编造路径"
        return True, "plan_context 就绪且 route 为空"
    if retry_pending(s, AGENT_ROUTE):
        return True, f"route 校验未通过，重试（{_flagged(s).get(AGENT_ROUTE)}）"
    return False, "route 已生成"


def _act_agent_precheck(s: Dict[str, Any]) -> Tuple[bool, str]:
    if not _ctx(s):
        return False, "plan_context 未生成"
    if not _precheck(s):
        return True, "plan_context 就绪且 precheck 为空"
    if retry_pending(s, AGENT_PRECHECK):
        return True, f"precheck 校验未通过，重试（{_flagged(s).get(AGENT_PRECHECK)}）"
    return False, "precheck 已生成"


def _act_plan_arbiter(s: Dict[str, Any]) -> Tuple[bool, str]:
    if not _ctx(s):
        return False, "plan_context 未生成"
    if _plan(s):
        if retry_pending(s, PLAN_ARBITER):
            return True, f"current_plan 校验未通过，重试（{_flagged(s).get(PLAN_ARBITER)}）"
        return False, "current_plan 已生成"
    if not _route_ready(s):
        return False, "route 未就绪（运力匹配拿不到 distance_km）"
    if not _precheck(s):
        return False, "precheck 未就绪"
    return True, "route 与 precheck 均已就绪"


def _act_approval_gate(s: Dict[str, Any]) -> Tuple[bool, str]:
    # ① 校验未通过的升级闸口优先：此时可能连 plan 都没产出来，不能先要求 current_plan
    nh = _needs_human(s)
    if nh and not _esc_answered(s):
        return True, f"产出校验未通过，需人工确认是否继续：{sorted(nh)}"
    if not _plan(s):
        return False, "current_plan 未生成"
    if not _accepted(s).get(PLAN_ARBITER):
        return False, "plan 尚未验收 accept"
    if not needs_approval(s):
        return False, "非高危（非 P0/P1），无需人工审批"
    if _approved(s):
        return False, "已审批通过"
    return True, "P0/P1 高危且未审批"


def _act_execute(s: Dict[str, Any]) -> Tuple[bool, str]:
    if not _plan(s):
        return False, "current_plan 未生成"
    if not _accepted(s).get(PLAN_ARBITER):
        return False, "plan 尚未验收 accept"
    # ⚠️ 业务不变量 ①：校验未通过的产出**不得下发**，必须由升级闸口拿到放行。
    nh = _needs_human(s)
    if nh and not _esc_answered(s):
        return False, f"产出校验未通过且未经人工确认——不得下发（{sorted(nh)}）"
    if nh and not _approved(s):
        return False, f"产出校验未通过且人工未放行——不得下发（{sorted(nh)}）"
    # ⚠️ 业务不变量 ②：高危未审批**一律不得下发**。这条不依赖模型自觉，
    # 是 supervisor 能做主的硬边界（漏掉它 = 绕过人工闸口）。
    if needs_approval(s) and not _approved(s):
        return False, "高危未审批——不得下发"
    if s.get("execution"):
        return False, "已下发运单（含未成功回执：挂起待补，不重复下发）"
    return True, "审批与验收均已满足，可下发"


ACTIONS: Dict[str, Callable[[Dict[str, Any]], Tuple[bool, str]]] = {
    PLAN_PREPARE: _act_plan_prepare,
    AGENT_ROUTE: _act_agent_route,
    AGENT_PRECHECK: _act_agent_precheck,
    PLAN_ARBITER: _act_plan_arbiter,
    APPROVAL_GATE: _act_approval_gate,
    EXECUTE: _act_execute,
}

# 推进优先级。agent_route / agent_precheck 是**同批并行**的一对（互不依赖），
# 由 decide() 特判；其余按此序单步推进。
ORDER = (PLAN_PREPARE, AGENT_ROUTE, AGENT_PRECHECK, PLAN_ARBITER,
         APPROVAL_GATE, EXECUTE)

# 可并行的一对：路线规划与前置取证互不依赖（运力匹配依赖 distance_km、
# 成本精算依赖选中承运商的 cost_per_km 属链式依赖，只能在仲裁点按序求解）
PARALLEL_PAIR = (AGENT_ROUTE, AGENT_PRECHECK)


def eligible(state: Dict[str, Any]) -> Dict[str, str]:
    """当前合法可执行集合：`{动作名: 依据}`。不在返回字典里的动作一律不可执行。"""
    out: Dict[str, str] = {}
    for name, judge in ACTIONS.items():
        ok, why = judge(state)
        if ok:
            out[name] = why
    return out


def handoff_target(state: Dict[str, Any]) -> str:
    """ELIGIBLE 为空时该去哪 —— 唯一出口，绝不返回空。

    * 已下发（`execution` 存在）-> `monitor`：规划段结束，交回执行段静态拓扑。
      实际上 execute 自己有静态出边 `execute -> monitor`，本分支是防御性的；
    * 出现 `needs_human` 但闸口没进得去（通常因为 plan 都没产出）-> `approval_gate`：
      不能静默把"没跑完"当"跑完了"，必须让人看到；
    * 其余 -> `end`（由调用方同时打错误日志告警，留痕在 supervisor_trace）。
    """
    if state.get("execution"):
        return HANDOFF_MONITOR
    if _needs_human(state) and not _esc_answered(state):
        return APPROVAL_GATE
    return HANDOFF_END


def known_targets() -> tuple:
    """supervisor 允许路由到的全部目标（供 decide 在返回 Command 前做硬校验）。

    为什么必须自己校验：实测官方引擎对 `Command.goto` 里的**未知目标静默忽略**
    （只往 branch 通道写、没有消费者的写不报错），结果是图提前终止而没有任何错误——
    把"没跑完"伪装成"跑完了"。所以目标合法性只能在返回 Command 之前挡住。
    """
    return tuple(ACTIONS) + HANDOFF


def stall_reasons(state: Dict[str, Any]) -> List[str]:
    """ELIGIBLE 为空时，逐个动作给出"为什么不能做"。仅用于诊断留痕，不参与路由。"""
    out: List[str] = []
    for name, judge in ACTIONS.items():
        ok, why = judge(state)
        if not ok:
            out.append(f"{name}: {why}")
    return out
