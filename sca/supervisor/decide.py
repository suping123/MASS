"""Supervisor 的决策函数 —— **纯函数**。

**红线（务必保持）**：本模块不写状态、不发通知、不落盘，只读 `state` 返回决策。
因为同一份决策逻辑要被两个适配器消费：

  * live   适配器 -> 转成 `Command(goto=...)`，真的改变路由；
  * shadow 适配器 -> 只写 `supervisor_trace`，路由仍走静态边。

影子模式是"先观察、不生效"的唯一实现方式；一旦把决策写进 `Command` 构造里，
它就做不成了。同理，**所有带随机性/外部依赖的东西都不能进这里**（模型调用可进，
但必须 fail-open、且只能从 ELIGIBLE 里挑）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .precondition import (EXECUTE, ORDER, PARALLEL_PAIR, eligible,
                           known_targets, needs_approval)

logger = logging.getLogger("sca.supervisor")


@dataclass
class Decision:
    """一次决策的完整留痕：目标 + 依据 + 置信度 + 被否掉的动作。

    `next_actions` 是**列表**：长度 > 1 表示这一批要并行派发（LangGraph 同超步语义）。
    空列表表示"无可执行动作"，由适配器决定如何收敛（live 走 END 并告警）。
    """
    next_actions: List[str] = field(default_factory=list)
    reason: str = ""
    rule: str = ""
    confidence: float = 0.0
    source: str = "rule"          # rule | model | fallback
    eligible: Dict[str, str] = field(default_factory=dict)
    rejected: List[str] = field(default_factory=list)
    defects: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"next_actions": list(self.next_actions), "reason": self.reason,
                "rule": self.rule, "confidence": self.confidence,
                "source": self.source, "eligible": dict(self.eligible),
                "rejected": list(self.rejected), "defects": list(self.defects)}


def decide_by_rule(state: Dict[str, Any]) -> Decision:
    """确定性决策：完全不调模型，同样输入必然同样输出（影子模式可复现比对）。"""
    elig = eligible(state)

    # ① 无合法动作 = 规划段与审批-下发分岔都已走完（主干由静态边接管）
    if not elig:
        return Decision([], "ELIGIBLE 为空：supervisor 职责段已完成，交回主干静态边",
                        "handoff", 1.0, "rule", elig)

    # ② 同批并行的一对同时就绪 -> 一起派出。
    #    这是 P1 扇出的保留点：拓扑上的并行必须由这里重新表达出来。
    pair = [a for a in PARALLEL_PAIR if a in elig]
    if len(pair) == len(PARALLEL_PAIR):
        return Decision(pair, "路线规划与前置取证互不依赖，同批并行派发",
                        "fanout_parallel", 1.0, "rule", elig)

    # ③ 按推进优先级单步走
    for action in ORDER:
        if action in elig:
            return Decision([action], f"按序推进：{action}（{elig[action]}）",
                            "sequential", 1.0, "rule", elig)

    # ④ 有合法动作但不在 ORDER 里（新增动作忘了登记）——宁可报出来也不静默乱走
    leftover = sorted(set(elig) - set(ORDER))
    return Decision([], f"ELIGIBLE 中的动作未登记进 ORDER：{leftover}",
                    "unregistered_action", 0.0, "fallback", elig, rejected=leftover)


def _ask_model(state: Dict[str, Any], base: Decision,
               model: Any) -> Optional[Decision]:
    """让模型在 ELIGIBLE 内做优先级判断（可选，fail-open）。

    契约：模型只允许返回 `eligible` 里已有的名字；越界一律丢弃并回退规则结果。
    当前竖切片默认关闭（`SCA_SUPERVISOR_MODEL=false`）——先把机制跑通并做到可复现，
    再打开模型；那时影子模式的一致率才是在衡量"模型与规则差在哪"。
    """
    try:
        picked = model(state, base)          # 由调用方注入，便于替换与测试
    except Exception as exc:  # noqa: 模型不可用绝不阻断调度
        logger.warning("[supervisor] 模型决策失败，回退规则结果：%s", exc)
        return None
    if not isinstance(picked, (list, tuple)):
        logger.warning("[supervisor] 模型返回形态非法（%r），回退规则结果", type(picked))
        return None
    allowed = set(base.eligible)
    valid = [str(x) for x in picked if str(x) in allowed]
    if not valid:
        return None
    return Decision(valid, "模型在 ELIGIBLE 内选定的优先级", "model_choice",
                    0.6, "model", base.eligible,
                    rejected=sorted(allowed - set(valid)))


def decide(state: Dict[str, Any], *, use_model: bool = False, model: Any = None,
           max_rounds: int = 12, rounds_used: int = 0) -> Decision:
    """Supervisor 决策入口。

    三层收敛保障（有界自主，缺一不可）：
      1. `ELIGIBLE` 为空 -> 主动交回主干（正常收敛）；
      2. 轮次耗尽     -> 强制交回主干并留下原因（防成环）；
      3. 图超步上限   -> 框架级兜底（`recursion_limit`，S1 已显式设置）。
    """
    if rounds_used >= max_rounds:
        base = decide_by_rule(state)
        return Decision([], f"已达 supervisor 最大决策轮次 {max_rounds}，强制交回主干",
                        "max_rounds_exceeded", 0.0, "fallback", base.eligible)

    base = decide_by_rule(state)
    if use_model and model is not None and base.eligible:
        picked = _ask_model(state, base, model)
        return picked or base
    return base


def validate_targets(actions: List[str]) -> List[str]:
    """返回非法目标（应为空）。适配器在构造 `Command` 之前调用。

    必要性：官方引擎对未知 goto 目标是**静默忽略**（图提前终止、不报错），
    等价于把"没跑完"伪装成"跑完了"。必须在返回 Command 前自己挡住。
    """
    allowed = set(known_targets()) | {"__end__"}
    return [a for a in actions if a not in allowed]
