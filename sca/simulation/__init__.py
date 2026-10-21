"""仿真数据引擎（原型期只做模拟论证，可行性报告 v2 §3.5 / §4.2）。

F1 全合成档位：生成模拟订单、回放 GPS 轨迹、注入异常。后续接 F2 时把
OrderGenerator 换成脱敏历史回放即可，接口不变。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("sca.simulation")


@dataclass
class OrderGenerator:
    """生成模拟多式联运订单（FR-01）。"""
    seq: int = 0

    def next(self) -> Dict[str, Any]:
        self.seq += 1
        oid = f"SO{self.seq:06d}"
        return {
            "order_id": oid,
            "cargo": {"type": random.choice(["冷链", "普货", "危险品"]), "weight_t": random.choice([2, 5, 12])},
            "time_window": {"start": "08:00", "end": "18:00"},
            "temp_ctrl": random.choice([True, False]),
            "route_hint": random.choice(["沪→京", "粤→鄂", "浙→川"]),
        }


@dataclass
class GPSTrackReplay:
    """回放 GPS 轨迹（FR-04）。projection 误差用于估算 ETA。"""

    def step(self, order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "lng": round(random.uniform(116.0, 121.0), 4),
            "lat": round(random.uniform(30.0, 40.0), 4),
            "speed": random.choice([60, 75, 90]),
            "ts": int(time.time()),
        }


@dataclass
class FaultInjector:
    """异常注入器（FR-05/FR-06）。从故障谱 FAULT_SPECTRUM 按 level 采样真实异常，
    保证异常码与 SOP 映射、子系统覆盖、自愈口径完全一致（修复早期 SOP-UNKNOWN）。
    level 决定是否触发 HITL（FR-08）。"""

    def inject(self, level: str | None = None) -> Dict[str, Any]:
        from sca.simulation.fault_spectrum import FAULT_SPECTRUM
        pool = [f for f in FAULT_SPECTRUM if (level is None or f.level == level)]
        f = random.choice(pool)
        return {
            "code": f.code, "name": f.name, "level": f.level, "desc": f.desc,
            "subsystems": list(f.subsystems), "auto": f.auto, "ts": int(time.time()),
        }

    def inject_code(self, code: str) -> Dict[str, Any]:
        """按指定异常码注入（用于文本识别出的具体故障，不再随机采样）。"""
        from sca.simulation.fault_spectrum import by_code
        f = by_code(code)
        if f is None:
            return self.inject(None)
        return {
            "code": f.code, "name": f.name, "level": f.level, "desc": f.desc,
            "subsystems": list(f.subsystems), "auto": f.auto, "ts": int(time.time()),
        }

    def needs_approval(self, level: str) -> bool:
        # FR-08：P0/P1 高危场景挂起审批
        return level in ("P0", "P1")
