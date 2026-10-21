"""MCP 工具层（FR-11 / FR-03 / FR-04）：气象 Server（真实气象数据，免 Key 可用）。

真实链路（按优先级）：
  0. ``SCA_AMAP_KEY`` 已配置 -> **高德天气**（restapi.amap.com/v3/weather/weatherInfo，
     与 map-mcp 路径规划**共用同一个 Key**，无需再申请和风）；
  1. ``SCA_QWEATHER_KEY`` 已配置 -> 和风天气真实 API（GeoAPI 城市 -> LocationID
     -> 3 天预报 / 实时官方预警；新版专属 Host 用 ``SCA_QWEATHER_HOST`` 指定）；
  2. 未配置 -> **Open-Meteo**（api.open-meteo.com，免费开放气象 API，无需账号/Key）：
     城市坐标（复用 map-mcp 的 45 城经纬度）-> 未来 2 天逐日预报
     （天气现象/气温/降水概率/降水量/最大风速）；预警由预报数据按阈值推导，
     诚实标注 ``warning_source=derived``（非官方预警）；
  3. Open-Meteo 不可达 -> 内置基线并标注 ``provider=builtin``，不冒充真实预报。

  * 结果做内存缓存（TTL 10 分钟），保住重规划 L0 <500ms 的延迟口径。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.weather")
mcp = FastMCP("weather-mcp") if HAVE_MCP else _StubMCP()

_TTL = 600  # 缓存 10 分钟（免费额度友好 + 低延迟）
_city_cache: Dict[str, Tuple[str, float]] = {}      # 城市 -> (location_id, ts)
_forecast_cache: Dict[str, Tuple[Dict, float]] = {}  # location_id -> (预报, ts)
_warning_cache: Dict[str, Tuple[Dict, float]] = {}    # location_id -> (预警, ts)
_amap_cache: Dict[str, Tuple[Dict, float]] = {}       # 城市 -> (高德预报, ts)
_host_cache: Optional[str] = None

_LEGACY_HOSTS = ("api.qweather.com", "devapi.qweather.com")


def _amap_key() -> str:
    """高德 Key（与 map-mcp 共用）：优先取「工具 Key 清单」，回退 settings/环境变量。"""
    try:
        from sca.credentials import first_credential
        c = first_credential("amap")
        k = ((c or {}).get("data") or {}).get("key", "").strip() if c else ""
        if k:
            return k
    except Exception:  # noqa
        pass
    try:
        from sca.settings import settings
        return (getattr(settings, "amap_key", "") or "").strip() \
            or os.environ.get("SCA_AMAP_KEY", "").strip()
    except Exception:
        return os.environ.get("SCA_AMAP_KEY", "").strip()


def _amap_forecast_with_key(city: str, key: str) -> Optional[Dict[str, Any]]:
    """用指定 Key 查高德天气预报（extensions=all，取最近一天 casts）。

    与和风分支返回相同结构；高德**不提供降水概率**，故 ``precip_prob=None``（不编造）。
    失败抛异常 / 返回 None，供凭证链判定降级。
    """
    if not key:
        return None
    hit = _amap_cache.get(city)
    if hit and time.time() - hit[1] < _TTL:
        return hit[0]
    q = urllib.parse.urlencode({"key": key, "city": city, "extensions": "all"})
    try:
        req = urllib.request.Request(
            f"https://restapi.amap.com/v3/weather/weatherInfo?{q}",
            headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=6) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[weather] 高德天气请求异常 %s（Key …%s）：%s", city, key[-4:], exc)
        raise
    if j.get("status") != "1":
        logger.warning("[weather] 高德天气返回 %s（%s）", j.get("info"), j.get("infocode"))
        return None
    casts = ((j.get("forecasts") or [{}])[0].get("casts") or [])
    if not casts:
        return None
    c = casts[0]

    def _f(v) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    fc = {"city": city, "condition": c.get("dayweather") or "",
          "temp_min_c": _f(c.get("nighttemp")), "temp_max_c": _f(c.get("daytemp")),
          "wind": f"{c.get('daywind', '')} {c.get('daypower', '')}级".strip(),
          "precip_prob": None, "date": c.get("date") or ""}
    _amap_cache[city] = (fc, time.time())
    return fc


async def _amap_forecast(city: str) -> Optional[Dict[str, Any]]:
    """高德天气查询：按「工具 Key 清单」依次尝试，失败自动降级下一条 Key。"""
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            k = (cred.get("data") or {}).get("key", "").strip()
            return await asyncio.to_thread(_amap_forecast_with_key, city, k)

        fc, _used, _att = await run_chain(KIND_TOOL, "amap", _one)
        return fc
    except Exception as exc:  # noqa: 凭证链不可用 -> 单点回退
        logger.warning("[weather] 凭证链不可用（%s），回退单 Key", exc)
        return await asyncio.to_thread(_amap_forecast_with_key, city, _amap_key())


async def _amap_route_forecast(cities: List[str]) -> Optional[Dict[str, Any]]:
    """高德路线预报：途经城市逐一查，结构对齐和风/Open-Meteo 分支。"""
    if not _amap_key():
        return None
    out: List[Dict[str, Any]] = []
    for c in cities[:4]:
        fc = await _amap_forecast(c)
        if fc:
            out.append(fc)
    if not out:
        return None
    worst = max(out, key=lambda x: float(x.get("precip_prob") or 0))
    tmax = [x["temp_max_c"] for x in out if x.get("temp_max_c") is not None]
    tmin = [x["temp_min_c"] for x in out if x.get("temp_min_c") is not None]
    avg = round(((max(tmax) if tmax else 22) + (min(tmin) if tmin else 15)) / 2, 1)
    return {"route": "→".join(cities), "cities": out, "condition": worst["condition"],
            "temp_c": avg, "wind": worst["wind"], "provider": "amap",
            "configured": True,
            "note": "高德官方预报（Web 服务）；高德不提供降水概率与官方气象预警"}


def _key() -> str:
    """和风 Key：settings 优先（支持前端/接口热更新），回退环境变量。"""
    try:
        from sca.settings import settings
        return (getattr(settings, "qweather_key", "") or "").strip() \
            or os.environ.get("SCA_QWEATHER_KEY", "").strip()
    except Exception:
        return os.environ.get("SCA_QWEATHER_KEY", "").strip()


def _configured_host() -> str:
    try:
        from sca.settings import settings
        return ((getattr(settings, "qweather_host", "") or "").strip()
                or os.environ.get("SCA_QWEATHER_HOST", "").strip()).rstrip("/")
    except Exception:
        return os.environ.get("SCA_QWEATHER_HOST", "").strip().rstrip("/")


def _detect_host(key: str = "", host: str = "") -> Optional[str]:
    """探测可用 API Host（新版专属 Host 未配置时按顺序尝试公共域名）。

    key/host 可显式传入（多 Key 降级时逐凭证探测）；留空则用全局配置，兼容旧调用。
    """
    global _host_cache
    k = key or _key()
    h = host or _configured_host()
    if h:
        return h if h.startswith("http") else f"https://{h}"
    if _host_cache:
        return _host_cache
    for cand in ("https://api.qweather.com", "https://devapi.qweather.com"):
        try:
            if _http_get(f"{cand}/v7/weather/now", {"location": "101010100", "key": k or "x"}):
                _host_cache = cand
                return cand
        except Exception:
            continue
    return None


def _http_get(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}",
                                 headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=6) as r:
        j = json.loads(r.read().decode("utf-8"))
    if j.get("code") not in ("200", 200):
        raise RuntimeError(f"qweather code={j.get('code')}")
    return j


def _geo_base(host: str) -> str:
    """GeoAPI 基址：公共域名走 geoapi.qweather.com，专属 Host 复用同一域名。"""
    if any(h in host for h in _LEGACY_HOSTS):
        return "https://geoapi.qweather.com"
    return host


def _lookup_city(city: str, key: str = "", host: str = "") -> Optional[str]:
    """城市名 -> 和风 LocationID（带缓存）；key/host 可显式传入以支持多 Key。"""
    hit = _city_cache.get(city)
    if hit and time.time() - hit[1] < _TTL * 24:
        return hit[0]
    h = host or _detect_host(key)
    if not h:
        return None
    try:
        j = _http_get(f"{_geo_base(h)}/v2/city/lookup",
                      {"location": city, "key": key or _key(), "number": 1})
        loc = (j.get("location") or [{}])[0]
        lid = loc.get("id")
        if lid:
            _city_cache[city] = (lid, time.time())
            return lid
    except Exception as exc:
        logger.warning("[weather] 城市查找失败 %s: %s", city, exc)
    return None


def _parse_cities(route: str) -> List[str]:
    """从路线串解析途经城市（"上海→北京" / "上海-苏州-北京" / 逗号分隔）。"""
    if not route:
        return []
    s = route.replace("→", "-").replace("->", "-").replace("—", "-") \
             .replace(",", "-").replace("，", "-").replace(" ", "")
    return [c for c in (x.strip() for x in s.split("-")) if c]


# ---------------- Open-Meteo（免 Key 真实气象） ----------------

_OM_BASE = "https://api.open-meteo.com/v1/forecast"
_om_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}   # "lat,lon" -> (预报, ts)

# WMO Weather interpretation codes -> 中文天气现象
_WMO_CN = {
    0: "晴", 1: "多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "雷暴伴冰雹",
}


def _beaufort(kmh: float) -> str:
    """风速 km/h -> 蒲福风级（约）。"""
    steps = [(1, "1级"), (6, "2级"), (12, "3级"), (20, "4级"), (29, "5级"),
             (39, "6级"), (50, "7级"), (62, "8级"), (75, "9级"), (89, "10级"),
             (103, "11级"), (118, "12级")]
    for lo, label in reversed(steps):
        if kmh >= lo:
            return label
    return "1级"


def _city_coord(city: str) -> Optional[Tuple[float, float]]:
    """城市名 -> (经度, 纬度)，复用 map-mcp 的 45 城坐标（懒加载避免环依赖）。"""
    try:
        from sca.mcp.map_server import _CITY_DB, _ALIAS
    except Exception:
        return None
    c = _ALIAS.get(city, city)
    if c in _CITY_DB:
        return _CITY_DB[c]
    for name in _CITY_DB:           # 包含匹配兜底（如 "北京市"）
        if city in name or name in city:
            return _CITY_DB[name]
    return None


def _om_daily(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Open-Meteo 未来 2 天逐日预报（取天气最差的一天），带 10 分钟缓存。"""
    key = f"{lat},{lon}"
    hit = _om_cache.get(key)
    if hit and time.time() - hit[1] < _TTL:
        return hit[0]
    params = {
        "latitude": lat, "longitude": lon, "timezone": "Asia/Shanghai",
        "forecast_days": 2,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,windspeed_10m_max",
    }
    url = f"{_OM_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=6) as r:
            j = json.loads(r.read().decode("utf-8"))
        d = j.get("daily") or {}
        codes = d.get("weathercode") or []
        if not codes:
            return None
        probs = d.get("precipitation_probability_max") or [0] * len(codes)
        sums = d.get("precipitation_sum") or [0] * len(codes)
        winds = d.get("windspeed_10m_max") or [0] * len(codes)
        tmax = d.get("temperature_2m_max") or [None] * len(codes)
        tmin = d.get("temperature_2m_min") or [None] * len(codes)
        # 取降水概率最高的一天（运输视角看最差天气）
        i = max(range(len(codes)), key=lambda k: float(probs[k] or 0))
        out = {"condition": _WMO_CN.get(int(codes[i]), f"天气码{codes[i]}"),
               "wmo_code": int(codes[i]),
               "temp_min_c": tmin[i], "temp_max_c": tmax[i],
               "precip_prob": float(probs[i] or 0),
               "precip_sum_mm": float(sums[i] or 0),
               "wind_kmh": float(winds[i] or 0), "wind": _beaufort(float(winds[i] or 0)),
               "date": (d.get("time") or [""])[i]}
        _om_cache[key] = (out, time.time())
        return out
    except Exception as exc:
        logger.warning("[weather] Open-Meteo 查询失败 %s: %s", key, exc)
        return None


def _om_route_forecast(cities: List[str]) -> Optional[Dict[str, Any]]:
    """Open-Meteo 路线预报：途经城市逐一查，返回与和风分支相同的结构。"""
    out: List[Dict[str, Any]] = []
    for c in cities[:4]:
        coord = _city_coord(c)
        if not coord:
            continue
        fc = _om_daily(coord[1], coord[0])
        if fc:
            out.append({"city": c, **fc})
    if not out:
        return None
    worst = max(out, key=lambda x: x.get("precip_prob") or 0)
    tmax = [x["temp_max_c"] for x in out if x.get("temp_max_c") is not None]
    tmin = [x["temp_min_c"] for x in out if x.get("temp_min_c") is not None]
    avg = round(((max(tmax) if tmax else 22) + (min(tmin) if tmin else 15)) / 2, 1)
    return {"route": "→".join(cities), "cities": out, "condition": worst["condition"],
            "temp_c": avg, "wind": worst["wind"], "provider": "open-meteo",
            "configured": False,
            "note": "免 Key 真实气象（Open-Meteo）；配置 SCA_AMAP_KEY 或 SCA_QWEATHER_KEY 可切换官方数据源"}


def _om_derived_warning(region: str) -> Optional[Dict[str, Any]]:
    """由 Open-Meteo 预报推导预警（诚实标注 derived，非官方预警）。

    阈值：雷暴码 -> 雷暴（橙）；暴雨码或日降水 >=50mm -> 暴雨（橙）；
    降水概率 >=70% 且雨码 -> 降雨（黄）；最大风速 >=62km/h（8 级）-> 大风（黄）。
    """
    coord = _city_coord(region)
    if not coord:
        return None
    fc = _om_daily(coord[1], coord[0])
    if not fc:
        return None
    code, prob, psum, wind = fc["wmo_code"], fc["precip_prob"], fc["precip_sum_mm"], fc["wind_kmh"]
    if code in (95, 96, 99):
        w, lv = "雷暴预警", "orange"
    elif code in (65, 82) or psum >= 50:
        w, lv = "暴雨预警", "orange"
    elif wind >= 62:
        w, lv = "大风预警", "yellow"
    elif prob >= 70 and code in (51, 53, 55, 61, 63, 80, 81):
        w, lv = "降雨提示", "yellow"
    elif code in (71, 73, 75, 85, 86):
        w, lv = "降雪提示", "yellow"
    else:
        w, lv = "无", "none"
    return {"warning": w, "level": lv, "provider": "open-meteo",
            "warning_source": "derived",
            "basis": f"{fc['date']} {fc['condition']} 降水概率{prob:.0f}% "
                     f"降水{psum:.1f}mm 风{fc['wind_kmh']:.0f}km/h",
            "note": "由 Open-Meteo 真实预报按阈值推导（非官方预警）；"
                    "高德 Web 服务无官方预警接口，如需官方预警请配置 SCA_QWEATHER_KEY"}


# ---------------------------------------------------------------------------
# 和风：多 Key 凭证链（单条 Key 失败自动降级下一条）
# ---------------------------------------------------------------------------
def _qw_route_forecast_sync(key: str, host: str,
                            cities: List[str]) -> Optional[Dict[str, Any]]:
    """用指定和风凭证查路线预报（同步阻塞，供 to_thread 调用）。"""
    h = _detect_host(key, host)
    if not h:
        return None
    out: List[Dict[str, Any]] = []
    for c in cities[:4]:
        lid = _lookup_city(c, key, h)
        if not lid:
            continue
        hit = _forecast_cache.get(lid)
        if hit and time.time() - hit[1] < _TTL:
            out.append(hit[0])
            continue
        try:
            j = _http_get(f"{h}/v7/weather/3d", {"location": lid, "key": key})
        except Exception as exc:
            logger.warning("[weather] 和风预报失败（Key …%s）：%s", key[-4:], exc)
            raise  # 抛给凭证链：切换下一条 Key
        d = (j.get("daily") or [{}])[0]
        fc = {"city": c, "condition": d.get("textDay", ""),
              "temp_min_c": float(d.get("tempMin", 0) or 0),
              "temp_max_c": float(d.get("tempMax", 0) or 0),
              "wind": f"{d.get('windScaleDay', '')}级",
              "precip_prob": d.get("precip", "0.0")}
        _forecast_cache[lid] = (fc, time.time())
        out.append(fc)
    if not out:
        return None
    worst = max(out, key=lambda x: float(x.get("precip_prob") or 0))
    return {"route": "→".join(cities), "cities": out, "condition": worst["condition"],
            "temp_c": round((worst["temp_min_c"] + worst["temp_max_c"]) / 2, 1),
            "wind": worst["wind"], "provider": "qweather", "configured": True}


async def _qw_route_forecast(cities: List[str]) -> Optional[Dict[str, Any]]:
    """和风预报：按凭证清单依次尝试，失败自动降级下一条 Key。"""
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            d = cred.get("data") or {}
            return await asyncio.to_thread(
                _qw_route_forecast_sync, (d.get("key") or "").strip(),
                (d.get("host") or "").strip(), cities)

        r, _used, _att = await run_chain(KIND_TOOL, "qweather", _one)
        return r
    except Exception as exc:  # noqa: 凭证链不可用 -> 单点回退
        logger.warning("[weather] 和风凭证链不可用（%s），回退单 Key", exc)
        return await asyncio.to_thread(_qw_route_forecast_sync, _key(),
                                       _configured_host(), cities)


def _qw_warning_sync(key: str, host: str, region: str) -> Optional[Dict[str, Any]]:
    """用指定和风凭证查官方预警（同步阻塞）。"""
    h = _detect_host(key, host)
    lid = _lookup_city(region, key, h) if h else None
    if not h or not lid:
        return None
    hit = _warning_cache.get(lid)
    if hit and time.time() - hit[1] < _TTL:
        return hit[0]
    try:
        j = _http_get(f"{h}/v7/warning/now", {"location": lid, "key": key})
    except Exception as exc:
        logger.warning("[weather] 和风预警失败（Key …%s）：%s", key[-4:], exc)
        raise
    warns = j.get("warning") or []
    if not warns:
        r = {"warning": "无", "level": "none"}
    else:
        w = warns[0]
        r = {"warning": f"{w.get('typeName', '')}{w.get('level', '')}".strip() + "预警"
                        if w.get("typeName") else "气象预警",
             "level": (w.get("level") or "").lower() or "unknown",
             "title": w.get("title", ""), "text": (w.get("text") or "")[:200]}
    _warning_cache[lid] = (r, time.time())
    return r


async def _qw_warning(region: str) -> Optional[Dict[str, Any]]:
    """和风预警：按凭证清单依次尝试，失败自动降级下一条 Key。"""
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            d = cred.get("data") or {}
            return await asyncio.to_thread(
                _qw_warning_sync, (d.get("key") or "").strip(),
                (d.get("host") or "").strip(), region)

        r, _used, _att = await run_chain(KIND_TOOL, "qweather", _one)
        return r
    except Exception as exc:  # noqa
        logger.warning("[weather] 和风凭证链不可用（%s），回退单 Key", exc)
        return await asyncio.to_thread(_qw_warning_sync, _key(),
                                       _configured_host(), region)


@mcp.tool()
async def get_forecast(route: str) -> dict:
    """获取路线途经城市天气预报：高德 Key 优先 -> 和风 -> Open-Meteo 免 Key -> 内置基线。"""
    cities = _parse_cities(route)
    if not cities:
        return {"route": route, "cities": [], "condition": "多云", "temp_c": 22,
                "wind": "3级", "provider": "builtin",
                "configured": bool(_amap_key() or _key())}
    # ① 高德（与路径规划共用「工具 Key 清单」中的 amap 凭证，无需额外申请账号）
    amap = await _amap_route_forecast(cities)
    if amap:
        amap["route"] = route
        return amap
    if not _key():
        # 免 Key 真实气象：Open-Meteo（无需账号），不可达才回退内置基线
        om = await asyncio.to_thread(_om_route_forecast, cities)
        if om:
            om["route"] = route
            return om
        return {"route": route, "cities": [], "condition": "多云", "temp_c": 22,
                "wind": "3级", "provider": "builtin", "configured": False,
                "note": "Open-Meteo 暂不可达，回退内置基线（本次非真实预报）"}
    # ② 和风：按凭证清单依次尝试（单条 Key 失败自动降级下一条）
    qw = await _qw_route_forecast(cities)
    if qw:
        qw["route"] = route
        return qw
    # 有凭证但全部失败 -> 不再伪造，如实返回并给出降级路径
    om = await asyncio.to_thread(_om_route_forecast, cities)
    if om:
        om["route"] = route
        om["note"] = "和风全部凭证不可用（已按清单降级重试），改由 Open-Meteo 真实预报提供"
        return om
    return {"route": route, "cities": [], "condition": "未知", "temp_c": None,
            "wind": "", "provider": "builtin", "configured": True,
            "note": "和风全部凭证失败且 Open-Meteo 不可达（本次非真实预报）"}


@mcp.tool()
async def get_warning(region: str) -> dict:
    """获取城市气象预警：和风官方预警（配 Key 时）-> Open-Meteo 预报推导（免 Key）-> 内置。

    注：高德 Web 服务**不提供官方气象预警**接口，故 `SCA_AMAP_KEY` 只影响预报（get_forecast），
    不改变预警链路；预警仍按阈值推导并标注 `warning_source=derived`（非官方预警）。
    """
    if not region:
        return {"region": region, "warning": "无", "level": "none",
                "provider": "builtin", "configured": bool(_amap_key() or _key())}
    if not _key():
        om = await asyncio.to_thread(_om_derived_warning, region)
        if om:
            return {"region": region, **om, "configured": bool(_amap_key())}
        return {"region": region, "warning": "无", "level": "none",
                "provider": "builtin", "configured": bool(_amap_key()),
                "note": "Open-Meteo 暂不可达，回退内置（非真实预警）"}
    # 和风官方预警：按凭证清单依次尝试（单条 Key 失败自动降级下一条）
    qw = await _qw_warning(region)
    if qw is not None:
        return {"region": region, **qw, "provider": "qweather", "configured": True}
    om = await asyncio.to_thread(_om_derived_warning, region)
    if om:
        return {"region": region, **om, "configured": True,
                "note": "和风全部凭证不可用（已按清单降级重试），改由真实预报阈值推导（非官方预警）"}
    return {"region": region, "warning": "无", "level": "none",
            "provider": "builtin", "configured": True,
            "note": "和风全部凭证失败且 Open-Meteo 不可达（非真实预警）"}


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
