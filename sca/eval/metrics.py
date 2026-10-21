"""指标定义与判定（L0/L1/L2 分层，v2 §3.5）。

status:
  pass = 达标
  fail = 未达标（硬性失败，须修复）
  na   = 需依赖外部能力验收（如模型 API / PG 向量库未就绪时的离线代理值），
         仅作开发期代理参考，不作为原型验收结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Metric:
    code: str
    name: str
    tier: str          # L0 / L1 / L2
    actual: float
    target: float
    unit: str
    status: str        # pass / fail / na
    direction: str = "ge"   # ge=实际≥目标 / le=实际≤目标
    note: str = ""

    @staticmethod
    def make(
        code: str, name: str, tier: str, actual: float,
        target: float, unit: str, direction: str, status: str = "pass", note: str = "",
    ) -> "Metric":
        if status == "na":
            return Metric(code, name, tier, round(actual, 4), target, unit, "na", direction, note)
        passed = (actual >= target) if direction == "ge" else (actual <= target)
        return Metric(code, name, tier, round(actual, 4), target, unit,
                      "pass" if passed else "fail", direction, note)

    def __str__(self) -> str:
        sym = {"pass": "✅", "fail": "❌", "na": "⚠️"}.get(self.status, "?")
        arrow = "≥" if self.direction == "ge" else "≤"
        tag = "" if self.status != "na" else "（离线代理，需外部能力验收）"
        return (f"  {sym} [{self.tier}] {self.name}: {self.actual}{self.unit} "
                f"(目标 {arrow}{self.target}{self.unit}){tag}")
