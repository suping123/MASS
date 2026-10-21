"""纯逻辑决策核心（无外部依赖，langgraph 节点与仿真评估引擎共用）。

对应需求：§6 执行层处置、FR-07 动态重规划、FR-08 HITL 人工审批、FR-10 SOP 命中、
L0 重规划 <500ms 口径（v2 §3.4）。所有函数确定性、可单测、可离线运行。
"""
from __future__ import annotations

from typing import Dict, List

# FR-08：P0/P1 高危场景挂起人工审批（LangGraph interrupt）
APPROVAL_LEVELS = ("P0", "P1")

# 规则快路径重规划耗时预算（毫秒），L0 <500ms 验收口径（v2 §3.4）
# 仅 P2/P3 可枚举异常 + P1 重规划走规则快路径可达；LLM 路径不计入 500ms 验收
REPLAN_BUDGET_MS: Dict[str, float] = {
    "P2": 200.0,
    "P3": 120.0,
    "P1": 350.0,
}

# 工具注册表（供 tongyi-intent-detect-v3 的 INTENT_MODE 与意图规则共用，FR-12）
TOOL_REGISTRY: List[Dict[str, str]] = [
    {"name": "order.query", "description": "查询运单状态"},
    {"name": "order.create", "description": "创建运单并下发"},
    {"name": "capacity.match", "description": "按货物/路线匹配运力 Top-N"},
    {"name": "plan.replan", "description": "动态重规划路径"},
    {"name": "exception.detect", "description": "在途异常检测"},
    {"name": "weather.query", "description": "查询路线天气/预警"},
    {"name": "map.route", "description": "路径规划与 ETA 估算"},
    {"name": "gps.track", "description": "GPS/IoT 遥测回放"},
    {"name": "knowledge.search", "description": "SOP/知识库检索"},
    {"name": "notify.send", "description": "通知（钉钉/邮件）"},
]


def requires_approval(level: str) -> bool:
    """FR-08：P0/P1 触发人工审批（状态机挂起等待 resume）。"""
    return level in APPROVAL_LEVELS


def route_action(level: str) -> str:
    """条件边路由：高危(P0/P1)挂起审批，其余(P2/P3)系统自动换路（自愈）。"""
    return "approval" if level in APPROVAL_LEVELS else "replan"


def estimate_replan_ms(level: str) -> float:
    """规则快路径重规划耗时（确定性预算，用于仿真论证 L0 <500ms）。"""
    return REPLAN_BUDGET_MS.get(level, 250.0)


def sla_target(level: str) -> str:
    return "human" if level in APPROVAL_LEVELS else "auto"
