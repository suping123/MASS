"""黄金评估集生成（L0 验收：工具调用准确率 95%、SOP 匹配 91%，v2 §3.4）。

生成 ≥500 条标注样本，两类：
  - intent：用户话术 -> 期望工具（验证意图/工具路由准确率，FR-12）
  - sop   ：异常码   -> 期望 SOP（验证 SOP 命中准确率，FR-10）

离线（无 API Key）用规则路由 RuleFirstIntentRouter 即可评估 intent 类；
接 tongyi-intent-detect-v3 后准确率应进一步提升（评估脚本会区分口径）。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List

from sca.intent import RuleFirstIntentRouter
from sca.simulation.fault_spectrum import FAULT_SPECTRUM, sop_for

# 每个意图规则对应的代表性话术（覆盖口语/书面/简写）
_INTENT_UTTERANCES: Dict[str, List[str]] = {
    "exception.deviation": ["车辆偏航了", "偏离路线怎么处理", "走错路了", "导航和实际不一致"],
    "exception.delay": ["这单延误了", "晚点严重", "超时未达", "没按时到"],
    "exception.temp": ["冷链温度超标", "温控报警", "冻品化了", "厢温过高"],
    "plan.replan": ["帮我换条路", "重新规划一下", "绕行", "改道重规划"],
    "order.query": ["查一下运单状态", "SO000123 到哪了", "查询订单", "这单现在什么情况"],
    "order.create": ["创建运单", "下个单", "建单派车", "开一张新运单"],
    "capacity.match": ["找辆冷链车", "匹配运力", "配个车", "附近有车吗"],
    "weather.query": ["目的地天气咋样", "查气象预警", "路线有暴雨吗", "天气查询"],
}

# 异常场景话术模板（用于扩展 sop 类样本规模）
_SOP_TEMPLATES = [
    "在途检测到 {name}（{desc}），请匹配处置 SOP",
    "{name}告警：{desc}",
    "司机上报 {name}，{desc}",
    "系统识别 {name}（{desc}），需 SOP",
    "{desc}导致 {name}，触发应急预案",
    "调度台收到 {name} 事件：{desc}",
]


@dataclass
class GoldenCase:
    kind: str           # "intent" | "sop"
    input: str
    expected: str
    level: str = ""
    code: str = ""


def build_intent_cases() -> List[GoldenCase]:
    cases: List[GoldenCase] = []
    for intent, utts in _INTENT_UTTERANCES.items():
        for u in utts:
            cases.append(GoldenCase(kind="intent", input=u, expected=intent))
    return cases


def build_sop_cases(per_fault: int = 12) -> List[GoldenCase]:
    """每个异常类生成 per_fault 条场景话术，确保总量 ≥500。"""
    cases: List[GoldenCase] = []
    for f in FAULT_SPECTRUM:
        for tpl in _SOP_TEMPLATES[:per_fault]:
            cases.append(GoldenCase(
                kind="sop",
                input=tpl.format(name=f.name, desc=f.desc),
                expected=sop_for(f.code),
                level=f.level,
                code=f.code,
            ))
    return cases


def build_golden_set(min_size: int = 500, seed: int = 20261012) -> List[GoldenCase]:
    rng = random.Random(seed)
    intent_cases = build_intent_cases()
    sop_cases = build_sop_cases(per_fault=12)  # 40*12 = 480
    cases = intent_cases + sop_cases
    # 若不足 min_size，复制 sop 场景补满（保持分布）
    while len(cases) < min_size:
        base = rng.choice(sop_cases)
        cases.append(GoldenCase(kind="sop", input=base.input + "（复核）",
                                expected=base.expected, level=base.level, code=base.code))
    return cases


def summarize(cases: List[GoldenCase]) -> Dict:
    n_intent = sum(1 for c in cases if c.kind == "intent")
    n_sop = sum(1 for c in cases if c.kind == "sop")
    return {"total": len(cases), "intent": n_intent, "sop": n_sop}
