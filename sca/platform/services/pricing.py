"""计价与结算：两侧服务费与承运商收益。

金额口径（全链路唯一解释，改这里就是改口径）：

    货主预算 gross
      ├─ 货主侧服务费 shipper_fee = gross × 2%
      └─ 挂牌运费 publish       = gross − shipper_fee      ← 承运商在公共池看到的价
            ├─ 承运商侧服务费 carrier_fee = publish × 2%
            └─ 承运商实收 income      = publish − carrier_fee

    承运商收益 profit = publish − carrier_fee − 运输成本
    运输成本 cost = 里程 × 承运商自报的每公里单价

守恒关系（结算时校验）：gross = shipper_fee + carrier_fee + carrier_income
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict

from .. import config

CENT = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def settle_amounts(gross: Decimal,
                   shipper_rate: Decimal | None = None,
                   carrier_rate: Decimal | None = None) -> Dict[str, Decimal]:
    """把货主预算拆成服务费、挂牌价、承运商实收。"""
    sr = shipper_rate if shipper_rate is not None else Decimal(str(config.SHIPPER_FEE_RATE))
    cr = carrier_rate if carrier_rate is not None else Decimal(str(config.CARRIER_FEE_RATE))
    g = _q(Decimal(gross))
    shipper_fee = _q(g * sr)
    publish = _q(g - shipper_fee)
    carrier_fee = _q(publish * cr)
    income = _q(publish - carrier_fee)
    return {"gross": g, "shipper_rate": sr, "shipper_fee": shipper_fee,
            "publish": publish, "carrier_rate": cr, "carrier_fee": carrier_fee,
            "carrier_income": income}


def carrier_profit(publish: Decimal, distance_km: Decimal,
                   cost_per_km: Decimal,
                   carrier_rate: Decimal | None = None,
                   deadhead_km: Decimal | float = 0) -> Dict[str, Decimal]:
    """承运商视角：这单接了我能挣多少。

    成本分两段，必须分开记：
      * 干线成本 linehaul = 订单里程 × 单价（有收入的那段）；
      * 空驶成本 deadhead = (当前位置→起运地) 里程 × 单价（纯支出那段）。
    只扣干线会让离起点远的承运商看到虚高的收益，接单后实际亏钱。
    """
    cr = carrier_rate if carrier_rate is not None else Decimal(str(config.CARRIER_FEE_RATE))
    p = _q(Decimal(publish))
    fee = _q(p * cr)
    rate = Decimal(cost_per_km or 0)
    linehaul = _q(Decimal(distance_km or 0) * rate)
    dh_km = Decimal(str(deadhead_km or 0))
    deadhead = _q(dh_km * rate)
    total_cost = _q(linehaul + deadhead)
    return {"quoted": p, "carrier_fee": fee,
            "linehaul_cost": linehaul, "deadhead_km": _q(dh_km),
            "deadhead_cost": deadhead, "est_cost": total_cost,
            "profit": _q(p - fee - total_cost)}


def check_conservation(s: Dict[str, Decimal]) -> bool:
    """结算守恒：货主实付 = 两面服务费 + 承运商实收。差一分钱都算错。"""
    return _q(s["shipper_fee"] + s["carrier_fee"] + s["carrier_income"]) == s["gross"]


def as_float(d: Dict[str, Any]) -> Dict[str, Any]:
    """给前端/JSON 用。仅用于展示，不参与再计算。"""
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in d.items()}
