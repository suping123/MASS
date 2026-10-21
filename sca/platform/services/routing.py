"""路线估算与空驶里程（离线）。

第一版用城市坐标做大圆距离 × 公路绕行系数，替代 GPS/地图链路：
  * 确定性、无网络、可单测 —— 调度方案不会因外部服务抖动而变化；
  * `provider` 字段如实标为 `offline-estimate`，不冒充真实地图规划。

2026-10-17 增强：
  * **精确到市/县**：起讫归一到 `_CITY_DB` 的真实键（该库含 2000+ 县级条目），
    途经枢纽复用 `map_server._pick_hubs`（按最小绕行 + 最小间隔选点）。
  * **空驶里程**：承运商从当前位置开到订单起运地的那段路，单独算出来给计价用。

接入真实地图后只需替换 `estimate_route`，调用方不用改。
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

ROAD_FACTOR = 1.28   # 公路里程 / 直线距离，国内干线常见 1.2–1.35
SPEED_KMH = 60.0     # 干线平均车速


def _db() -> Dict[str, Tuple[float, float]]:
    from sca.mcp.map_server import _CITY_DB

    return _CITY_DB


@lru_cache(maxsize=1)
def _keys_by_len() -> Tuple[str, ...]:
    """按长度降序的城市键，用于在自由文本里做最长匹配。"""
    return tuple(sorted(_db().keys(), key=len, reverse=True))


def _strip_suffix(s: str) -> str:
    for suf in ("市辖区", "自治州", "地区", "市", "县", "区", "镇"):
        if len(s) > len(suf) + 1 and s.endswith(suf):
            return s[: -len(suf)]
    return s


def resolve_city(name: Optional[str]) -> Optional[str]:
    """把任意地名归一为 `_CITY_DB` 里的真实键。

    顺序：精确 -> 去行政后缀 -> 加行政后缀 -> 包含匹配。
    最后一步多义时取长度最接近的，避免"南京"命中"南京北"这类奇怪键。
    """
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    db = _db()
    if s in db:
        return s
    base = _strip_suffix(s)
    if base in db:
        return base
    for suf in ("市", "县", "区"):
        if base + suf in db:
            return base + suf
    hits = [k for k in db if base and (base in k or k in base)]
    if not hits:
        return None
    return min(hits, key=lambda k: abs(len(k) - len(base)))


@lru_cache(maxsize=512)
def city_in_text(text: Optional[str]) -> Optional[str]:
    """从自由文本（如"石家庄正定物流园"）里提取城市键，取最长匹配。

    加缓存：公共池里每条订单都要用承运商的同一个位置算空驶，
    不缓存的话一次列表渲染会做几十次 2000+ 条的全量扫描。
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    direct = resolve_city(s)
    if direct and len(direct) >= 2 and direct in s:
        return direct
    for k in _keys_by_len():
        if len(k) >= 2 and k in s:
            return k
    return direct


def _coord(key: Optional[str]) -> Optional[Tuple[float, float]]:
    if not key:
        return None
    return _db().get(key)


def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    r = 6371.0
    (lng1, lat1), (lng2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _hubs(origin_key: str, dest_key: str) -> List[str]:
    """途经枢纽（县级粒度）。失败返回空列表——路线仍可用，只是没有途经点。"""
    try:
        from sca.mcp.map_server import _pick_hubs

        return list(_pick_hubs(origin_key, dest_key))
    except Exception:  # noqa: BLE001 选点失败不应让整条路线失败
        return []


def estimate_route(origin: Optional[str], dest: Optional[str],
                   with_waypoints: bool = True) -> Dict[str, Any]:
    """返回 {ok, distance_km, eta_h, waypoints, provider, reason?}。

    `with_waypoints=False` 用于高频场景（公共池收益快算）：跳过枢纽选点，
    只算两点距离，避免对 2000+ 城市反复遍历。
    """
    ko, kd = resolve_city(origin), resolve_city(dest)
    if not ko or not kd:
        miss = [n for n, k in ((origin, ko), (dest, kd)) if not k]
        return {"ok": False,
                "reason": f"无法识别城市：{'、'.join(str(m) for m in miss)}",
                "provider": "offline-estimate"}
    if ko == kd:
        return {"ok": False, "reason": "起运地与目的地相同，无需规划",
                "provider": "offline-estimate"}

    straight = _haversine(_coord(ko), _coord(kd))  # type: ignore[arg-type]
    km = round(straight * ROAD_FACTOR, 1)
    path = [origin, dest]
    if with_waypoints:
        path = [origin] + _hubs(ko, kd) + [dest]
    return {"ok": True, "distance_km": km,
            "eta_h": round(km / SPEED_KMH, 1),
            "provider": "offline-estimate",
            "waypoints": [str(p) for p in path],
            "origin_key": ko, "dest_key": kd,
            "hub_count": max(0, len(path) - 2)}


def deadhead(from_position: Optional[str], to_origin: Optional[str]) -> Dict[str, Any]:
    """空驶里程：承运商当前位置 -> 订单起运地。

    位置是承运商自报的文本（可能很自由，如"石家庄正定物流园"），先提取城市再算距离。
    提取不到时**返回 ok=False 并说明原因**，由调用方按 0 计并标注"空驶未计"——
    绝不猜一个位置，那等于替承运商编成本。
    """
    if not from_position:
        return {"ok": False, "reason": "承运商未提供当前位置", "deadhead_km": 0.0}
    kf = city_in_text(from_position)
    kt = resolve_city(to_origin)
    if not kf:
        return {"ok": False, "reason": f"无法从位置「{from_position}」识别城市",
                "deadhead_km": 0.0}
    if not kt:
        return {"ok": False, "reason": f"无法识别起运地「{to_origin}」", "deadhead_km": 0.0}
    if kf == kt:
        return {"ok": True, "deadhead_km": 0.0, "from_key": kf, "to_key": kt,
                "note": "已在起运地，无空驶"}
    km = round(_haversine(_coord(kf), _coord(kt)) * ROAD_FACTOR, 1)  # type: ignore[arg-type]
    return {"ok": True, "deadhead_km": km, "from_key": kf, "to_key": kt,
            "from_text": str(from_position)}
