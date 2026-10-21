"""LangGraph 状态定义。

状态字段严格对应需求文档 §6：订单、货物、当前计划、计划版本、运力、路线、
遥测、天气、异常、SOP、审批、成本、ETA、指标。另加 intent/route_decision/trace
用于意图路由与全链路可观测（FR-13）。

**通道语义（P0 状态通道化，2026-10-16）**：
  * 普通字段 = 覆盖语义（last-write-wins）。写它的节点在同一超步内**只能有一个**，
    否则 LangGraph 会抛 InvalidUpdateError —— 决策字段（current_plan / route /
    route_decision / approval）属于此类，写权限必须收口到单一节点。
  * `Annotated[..., reducer]` 字段 = 累加语义。多个并行分支各自只返回**本次新增**的
    元素即可，框架负责 fold；节点里再写 `list(s.get("x") or []) + [...]` 会重复累加。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def merge_dict(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """浅合并（后者覆盖同名键）——用于允许多个节点共同补充的字典通道。"""
    out = dict(a or {})
    out.update(b or {})
    return out


class SupplyChainState(TypedDict, total=False):
    order_id: str
    order: Dict[str, Any]                 # FR-01 多式联运订单
    source_text: str                      # 调度员原话（意图路由/复盘/案例沉淀用）
    affected_segment: str                 # 可选录入的受影响路段（应急重规划避让依据）
    cargo: Dict[str, Any]                 # 货物 / 温控 / 时间窗
    current_plan: Dict[str, Any]          # 当前运输计划
    plan_version: int                     # 计划版本（FR-07 动态重规划可追溯）
    capacity: List[Dict[str, Any]]        # 匹配到的运力 Top-N（FR-02）
    route: Dict[str, Any]                 # 路径（FR-03）
    telemetry: Dict[str, Any]             # GPS/IoT 遥测（FR-04）
    weather: Dict[str, Any]               # 气象（FR-03/FR-04）
    exception: Dict[str, Any]             # 异常事件（FR-05/FR-06）
    sop: Dict[str, Any]                   # 命中的 SOP（FR-10）
    approval: Dict[str, Any]              # HITL 审批（FR-08）
    dispatch_review: Dict[str, Any]       # 调度员下发审核（钉钉发送前闸口）
    cost: Dict[str, Any]                  # 成本（FR 成本与履约）
    eta: Dict[str, Any]                   # ETA（FR 实时在途）
    metrics: Dict[str, Any]               # 指标（FR-13 看板）
    intent: Dict[str, Any]                # 意图路由结果（FR-12）
    route_decision: Dict[str, Any]        # 工具路由决策
    execution: Dict[str, Any]             # 运单下发结果（tms 回执/失败原因）
    knowledge_updated: Dict[str, Any]     # 案例沉淀结果
    next_action: List[str]               # supervisor 的下一步目标（多个 = 同批并行派发）
    memory_blocks: List[Dict[str, Any]]   # 长短期记忆注入块（sca.memory 召回；数据非指令）

    # —— 累加型通道（reducer）：并行分支各自追加，互不覆盖 ——
    trace: Annotated[List[str], operator.add]                    # 全链路 Trace（FR-13）
    mcp_calls: Annotated[List[Dict[str, Any]], operator.add]     # MCP 工具调用证据（审计）
    notifications: Annotated[List[Dict[str, Any]], operator.add]  # 通知结果（含发送回执）
    agent_log: Annotated[List[Dict[str, Any]], operator.add]     # 各 Agent 执行记录（并行取证）

    # —— 规划段并行（P1）：prepare 产出的共享上下文 + 各 Agent 的分区产出 ——
    plan_context: Dict[str, Any]          # 规划上下文（起讫/货类/温控/危品/重量口径/smart 判定）
    precheck: Dict[str, Any]              # 并行前置取证（订单核验/货类合规检索）
    plan_arbitration: Dict[str, Any]      # 规划仲裁证据（各路结论/选定依据/降级原因）

    # —— Supervisor 化（S1 契约层）：决策与验收留痕 ——
    # 三个字段都是**覆盖语义 + 唯一写者**：并行分支不得写它们，否则 LangGraph 抛
    # InvalidUpdateError（这正是"决策字段写权限收口"的既有约定）。
    supervisor_decision: Dict[str, Any]   # 本轮决策（唯一写者：supervisor）
    evaluation: Dict[str, Any]            # 最近一次验收结论（唯一写者：evaluator）
    loop_count: int                       # supervisor 已决策轮次（有界自主的计量口径）

    # 决策序列：每次 supervisor 决策追加一条，供影子模式做决策一致率比对。
    # 用 reducer 是因为它在循环里会被反复写；且影子模式下与 live 决策**分开记**，
    # 两者才能逐条对齐比较。
    supervisor_trace: Annotated[List[Dict[str, Any]], operator.add]
