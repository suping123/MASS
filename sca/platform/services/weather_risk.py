"""天气风险：把"预报"变成"对时效与风险的结论"。

改造前的状况
------------
`weather-mcp` 能力是完整的（高德 → 和风 → Open-Meteo → 内置四级降级），但只在两处被
消费：内核 `monitor` 节点拉预报、`detect_exception` 的 R5 规则命中天气词。平台的
**路径规划完全不看天气**——`plan_route_smart` 的入参里连天气字段都没有，所以
"会不会下暴雨"从来没进过出方案的决策。

这个模块补的是中间那一层：预报 -> 风险系数 -> ETA。

三条原则
--------
1. **只上浮，不下调**。天气好时不把 ETA 往下压——预报不确定性大，压低时效等于
   给客户一个不可能兑现的承诺。
2. **不改变价格**。挂牌价是「预算 − 货主侧费率」的确定性公式，天气只影响时效与
   风险提示。若要收天气附加费，必须另立审批链（本轮不做）。
3. **查不到就说查不到**。provider=builtin 时 factor 归 1.0，并在方案里写明
   "天气未取到，按基准时效"，绝不假装"天气晴"。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..mcp_bridge import McpTimeout, run_async

logger = logging.getLogger("sca.platform.services.weather_risk")

# 天气现象 -> 时效上浮系数。按"关键词最多命中一条"取值，不做累加——
# 累加会把台风+暴雨算成 +37%，那种数字没人敢用来承诺时效。
_FACTOR_RULES = (
    ("台风", 0.25), ("飓风", 0.25),
    ("暴雪", 0.15), ("大雪", 0.12), ("中雪", 0.08), ("小雪", 0.04),
    ("暴雨", 0.12), ("大雨", 0.10), ("中雨", 0.06), ("阵雨", 0.03),
    ("冻雨", 0.10), ("雷暴", 0.10),
    ("大雾", 0.08), ("雾", 0.04),
    ("沙尘", 0.06), ("浮尘", 0.03),
    ("大风", 0.06),
)

# 高温只对温控货增加风险（厢温维持难度上升），普货不计
HIGH_TEMP_C = 35.0
HIGH_TEMP_FACTOR = 0.05

# 等级阈值：factor >= 0.10 判"中"，>= 0.20 判"高"
LEVEL_MID = 0.10
LEVEL_HIGH = 0.20


def _factor_of(condition: str, cargo_type: str,
               temp_max: Optional[float] = None) -> tuple[float, str]:
    """返回 (系数, 命中的天气现象)。命中多条时取第一条（表内已按严重度排序）。"""
    text = str(condition or "")
    for word, f in _FACTOR_RULES:
        if word in text:
            return f, word
    if cargo_type in ("医药冷链", "生鲜冷链", "冷链") and \
            temp_max is not None and temp_max >= HIGH_TEMP_C:
        return HIGH_TEMP_FACTOR, f"高温 {temp_max:.0f}℃"
    return 0.0, ""


def _level(factor: float) -> str:
    if factor >= LEVEL_HIGH:
        return "high"
    if factor >= LEVEL_MID:
        return "medium"
    if factor > 0:
        return "low"
    return "none"


async def _forecast_async(route: str) -> Dict[str, Any]:
    from sca.mcp.client import call as mcp_call

    r = await mcp_call("weather-mcp", "get_forecast", route=route)
    if r.ok and isinstance(r.data, dict):
        return r.data
    return {"provider": "unavailable", "error": r.error or "weather-mcp 不可用"}


def assess(cities: List[str], cargo_type: str = "普货") -> Dict[str, Any]:
    """查一条路线的天气风险，返回时效系数与结论。

    返回：
      {ok, provider, condition, cities, factor, level, level_label, hits, note}
    `ok=False` 表示没取到预报（此时 factor 一定是 1.0 的等价物 0.0）。
    """
    cities = [str(c).strip() for c in (cities or []) if str(c or "").strip()]
    if not cities:
        return {"ok": False, "provider": "none", "factor": 0.0, "level": "none",
                "condition": "", "note": "路线里没有可用城市，未查天气"}

    route = "→".join(cities[:4])     # weather-mcp 内部按 4 个城市采样
    try:
        fc = run_async(_forecast_async(route), timeout=20)
    except McpTimeout:
        return {"ok": False, "provider": "timeout", "factor": 0.0, "level": "none",
                "condition": "", "note": "天气查询超时，按基准时效"}
    except Exception as exc:  # noqa: BLE001 外部服务异常不阻塞出方案
        logger.warning("[weather_risk] 查询失败：%s", exc)
        return {"ok": False, "provider": "error", "factor": 0.0, "level": "none",
                "condition": "", "note": f"天气查询失败（{type(exc).__name__}），按基准时效"}

    provider = fc.get("provider") or "unknown"
    condition = str(fc.get("condition") or "")
    temp_max = fc.get("temp_max_c")
    if temp_max is None:
        temps = [c.get("temp_max_c") for c in (fc.get("cities") or [])
                 if c.get("temp_max_c") is not None]
        temp_max = max(temps) if temps else None

    factor, hit = _factor_of(condition, cargo_type, temp_max)
    ok = provider not in ("builtin", "unavailable", "none")
    lvl = _level(factor)

    hits: List[Dict[str, Any]] = []
    for c in (fc.get("cities") or []):
        f2, w2 = _factor_of(str(c.get("condition") or ""), cargo_type,
                            c.get("temp_max_c"))
        if f2 > 0:
            hits.append({"city": c.get("city"), "condition": c.get("condition"),
                         "factor": f2, "hit": w2})
    hits.sort(key=lambda x: -x["factor"])

    if not ok:
        note = "天气未取到（服务不可达），按基准时效计算，未计入天气上浮"
    elif factor <= 0:
        note = f"沿途天气 {condition or '未知'}，未发现影响运输的天气"
    else:
        note = (f"沿途{condition}（{hit}），预计时效上浮 {factor * 100:.0f}%；"
                f"{'已计入方案 ETA' if ok else '未计入'}")
    if provider == "amap":
        note += "；数据源：高德官方预报"

    return {"ok": ok, "provider": provider, "condition": condition,
            "cities": fc.get("cities") or [], "factor": factor,
            "level": lvl, "level_label": {"high": "高", "medium": "中",
                                          "low": "低", "none": "无"}[lvl],
            "hits": hits, "temp_max_c": temp_max, "note": note}


def apply_to_eta(base_eta_h: float, risk: Dict[str, Any]) -> tuple[float, float]:
    """把天气系数套到基准时效上。返回 (新时效, 实际生效的系数)。"""
    f = float(risk.get("factor") or 0.0) if risk.get("ok") else 0.0
    if base_eta_h <= 0:
        return base_eta_h, 0.0
    return round(base_eta_h * (1 + f), 1), f


__all__ = ["assess", "apply_to_eta", "HIGH_TEMP_C", "LEVEL_MID", "LEVEL_HIGH"]
