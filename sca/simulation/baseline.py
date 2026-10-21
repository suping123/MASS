"""L1 仿真基线假设（v2 §3.5，须业务方签核）。

所有 L1 仿真指标都以"人工流程基线 vs 本系统基线"之差来论证。基线参数即验收会的
审查对象（审假设合理性 + 脚本可复现，不审绝对值）。任何参数改动须更新本文件并重新发版。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Baseline:
    # ---- 响应时效（分钟）----
    manual_response_min: float = 30.0     # 人工响应 30min
    sys_response_min: float = 3.0         # 系统响应 3min（目标）

    # ---- 成本（元/单）----
    manual_cost_per_order: float = 3200.0
    sys_cost_per_order: float = 2816.0    # ≈ -12%

    # ---- 准时率 ----
    manual_ontime_rate: float = 0.885
    sys_ontime_rate: float = 0.965        # 96.5%

    # ---- 赔付率（异常单中发生赔付的比例）----
    manual_claim_rate: float = 0.060
    sys_claim_rate: float = 0.0348        # ≈ -42%

    # ---- 新承运商/子系统接入周期（天）----
    manual_integration_days: float = 25.0
    sys_integration_days: float = 10.0    # ≈ -60%

    # ---- 自愈率 ----
    manual_selfheal_rate: float = 0.0      # 纯人工无自愈
    sys_selfheal_target: float = 0.82

    # ---- 评估集规模 ----
    golden_set_target: int = 500

    # ---- 自动处置成功率假设（用于自愈率仿真，须业务方确认）----
    # P2/P3 系统自动换路/记录建议，成功率高；P1 重规划+人工确认，视为"半自动"，
    # 人工快速确认后处置成功率高；P0 必审批不计入自愈。该假设为验收审查对象。
    auto_success_rate: Dict[str, float] = field(default_factory=lambda: {
        "P2": 0.98, "P3": 0.99, "P1": 0.85, "P0": 0.0,
    })

    def as_dict(self) -> Dict:
        return {
            "manual_response_min": self.manual_response_min,
            "sys_response_min": self.sys_response_min,
            "manual_cost_per_order": self.manual_cost_per_order,
            "sys_cost_per_order": self.sys_cost_per_order,
            "manual_ontime_rate": self.manual_ontime_rate,
            "sys_ontime_rate": self.sys_ontime_rate,
            "manual_claim_rate": self.manual_claim_rate,
            "sys_claim_rate": self.sys_claim_rate,
            "manual_integration_days": self.manual_integration_days,
            "sys_integration_days": self.sys_integration_days,
            "manual_selfheal_rate": self.manual_selfheal_rate,
            "sys_selfheal_target": self.sys_selfheal_target,
            "golden_set_target": self.golden_set_target,
            "auto_success_rate": self.auto_success_rate,
        }


# 生产目标（L2，原型期不验收，仅登记）
L2_TARGETS = {
    "new_dispatcher_onboard_cycle_days": 14,
    "team_efficiency_gain_pct": 85,
}
