"""高德 / 和风 API Key 连通性自检（供 API 与 CLI 共用）。

原则（不编造、不假装成功）：
  - 未配置 Key：返回 configured=false + 精确到菜单的获取步骤，且**不发任何网络请求**；
  - 已配置：真实调用官方 API；失败时翻译官方 errcode 含义，不把失败说成成功。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from sca.settings import settings

AMAP_ERR = {
    "10001": "错误的 Key（Key 不存在或已删除）",
    "10002": "无此服务权限（Key 类型不是「Web 服务」，请重建 Key 并选 Web 服务）",
    "10003": "访问已超出配额（免费额度用尽）",
    "10004": "单位时间内访问过于频繁（稍后再试）",
    "10009": "Key 缺少调用权限（检查服务平台与所属应用）",
    "INVALID_USER_SCODE": "安全密钥/签名错误（Web 服务 Key 一般不需要安全密钥）",
    "INVALID_USER_KEY": "Key 无效或已被禁用",
    "DAILY_QUERY_OVER_LIMIT": "当日调用量超限",
}
QW_ERR = {
    "200": "请求成功", "204": "查询地区暂无数据", "400": "请求错误（参数缺失或格式错）",
    "401": "Key 无效或已过期", "402": "超过访问配额（免费额度用尽）",
    "403": "无访问权限（Key 未订阅该数据，或应用类型不是 Web API）",
    "404": "查询不到该城市", "429": "请求过于频繁", "500": "服务无响应",
}

AMAP_STEPS = [
    "打开 console.amap.com → 手机号注册/登录 → 个人开发者实名认证（身份证 + 人脸）",
    "应用管理 → 我的应用 → 创建新应用（名称随意，类型选「其他」）",
    "应用卡片 → 添加 Key → 服务平台务必选「Web 服务」（不要选 Web端(JS API)）",
    "复制 Key → 填入 .env 的 SCA_AMAP_KEY=，或在前端「系统设置 → 外部服务 Key」填写",
]
QW_STEPS = [
    "打开 console.qweather.com → 注册/登录 → 实名认证",
    "应用管理 → 创建应用 → 类型选「Web API」→ 订阅选「免费开发版」",
    "复制 Key → 填入 .env 的 SCA_QWEATHER_KEY=",
    "（新版账号）复制专属 API Host → 填入 SCA_QWEATHER_HOST=",
]


def _mask(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return (s[:6] + "…" + s[-4:]) if len(s) > 12 else "已配置"


def _open(url: str, timeout: int = 8) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _city(name: str):
    from sca.mcp import map_server as ms
    return ms._CITY_DB.get(name)


def check_amap() -> Dict[str, Any]:
    """真实调用高德驾车路径规划 v3 + 天气查询，验证 Key 可用性。

    同一个 Key 同时供 map-mcp（路径规划）与 weather-mcp（天气查询）。
    """
    key = (getattr(settings, "amap_key", "") or "").strip()
    out = {"service": "amap", "name": "高德地图（路径规划 + 天气查询）", "configured": bool(key),
           "key_masked": _mask(key), "steps": AMAP_STEPS}
    if not key:
        out.update(ok=False, message="未配置；当前用内置 2042 城中心点估算里程 + Open-Meteo 免 Key 预报")
        return out
    o, d = _city("上海"), _city("杭州")
    q = urllib.parse.urlencode({"key": key, "origin": f"{o[0]},{o[1]}",
                                "destination": f"{d[0]},{d[1]}", "extensions": "base"})
    try:
        j = _open(f"https://restapi.amap.com/v3/direction/driving?{q}")
    except Exception as exc:
        out.update(ok=False, message=f"请求异常：{exc}")
        return out
    if j.get("status") != "1":
        code = str(j.get("infocode") or j.get("errcode") or j.get("info") or "")
        out.update(ok=False, message=f"高德返回 {j.get('info')}（{code}）",
                   reason=AMAP_ERR.get(code, "见 https://lbs.amap.com/api/webservice/guide/tools/info"))
        return out
    path = ((j.get("route") or {}).get("paths") or [{}])[0]
    dist = round(float(path.get("distance", 0)) / 1000, 1)
    eta = round(float(path.get("duration", 0)) / 3600, 1)
    out.update(ok=True,
               message=f"上海→杭州 {dist} km / {eta} h（高德真实路网）",
               detail={"distance_km": dist, "eta_h": eta})
    # 同一 Key 顺带验证天气查询（weather-mcp 已切换为高德优先）
    try:
        w = _open("https://restapi.amap.com/v3/weather/weatherInfo?"
                  + urllib.parse.urlencode({"key": key, "city": "北京", "extensions": "all"}))
        cast = ((w.get("forecasts") or [{}])[0].get("casts") or [{}])[0]
        if w.get("status") == "1" and cast:
            out["weather_ok"] = True
            out["message"] += f"；天气 北京 {cast.get('dayweather')} " \
                              f"{cast.get('daytemp')}~{cast.get('nighttemp')}℃"
        else:
            out["weather_ok"] = False
            out["message"] += f"；天气查询失败（{w.get('info')}），已回退 Open-Meteo"
    except Exception as exc:
        out["weather_ok"] = False
        out["message"] += f"；天气查询异常：{exc}"
    return out


def check_qweather() -> Dict[str, Any]:
    """真实调用和风城市查询 + 实况天气，验证 Key 可用性。"""
    from sca.mcp import weather_server as ws
    key, host = ws._key(), ws._configured_host()
    out = {"service": "qweather", "name": "和风天气（预报/预警）", "configured": bool(key),
           "key_masked": _mask(key), "host": host or "", "steps": QW_STEPS}
    has_amap = bool((getattr(settings, "amap_key", "") or "").strip())
    if not key:
        out.update(ok=False, configured=False,
                   message=("未配置；天气已由高德 Key 提供，此项非必需（仅官方气象预警需要）"
                            if has_amap else "未配置；天气由 Open-Meteo 免 Key 真实预报提供"),
                   optional=has_amap)
        return out
    hosts: List[str] = ([host if host.startswith("http") else f"https://{host}"] if host
                        else ["https://api.qweather.com", "https://devapi.qweather.com"])
    last = None
    for h in hosts:
        try:
            geo = ("https://geoapi.qweather.com"
                   if any(x in h for x in ("api.qweather.com", "devapi.qweather.com")) else h)
            j = _open(f"{geo}/v2/city/lookup?{urllib.parse.urlencode({'location': '北京', 'key': key})}")
            lid = (j.get("location") or [{}])[0].get("id")
            if not lid:
                last = f"城市查询无结果（code={j.get('code')}）"
                continue
            w = _open(f"{h}/v7/weather/now?{urllib.parse.urlencode({'location': lid, 'key': key})}")
            now = w.get("now") or {}
            out.update(ok=True, host_used=h,
                       message=f"北京实况 {now.get('text')} {now.get('temp')}℃ "
                               f"（和风官方 · Host={h}）",
                       detail={"city": "北京", "text": now.get("text"),
                               "temp": now.get("temp"), "host": h})
            return out
        except Exception as exc:
            last = str(exc)
            continue
    code = last.split("code=")[-1].strip().rstrip(")") if (last and "code=" in last) else ""
    out.update(ok=False, message=last or "所有 Host 均不可用",
               reason=QW_ERR.get(code, "见 https://dev.qweather.com/docs/start/status-code"))
    return out


def check_all(which: str = "all") -> Dict[str, Any]:
    """自检全部项；标记为 optional 的项（如已有高德 Key 时的和风）不参与达标判定。"""
    items = []
    if which in ("all", "amap"):
        items.append(check_amap())
    if which in ("all", "qweather"):
        items.append(check_qweather())
    required = [i for i in items if not i.get("optional")]
    return {"items": items,
            "all_ok": bool(required) and all(i.get("ok") for i in required),
            "any_missing": any((not i.get("configured")) and not i.get("optional")
                               for i in items)}
