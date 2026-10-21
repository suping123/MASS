"""MCP 工具层（FR-11 / FR-04）：GPS / IoT 遥测 Server。

真实分层设计：
  * ``report_telemetry`` 是真实 IoT 上报入口（车载终端/北斗网关调用，落盘持久化）；
  * ``set_route`` 由规划层在确定运输计划后调用（对应生产 TMS→车载终端的路线下发）；
  * ``get_latest_position`` / ``get_telemetry`` / ``replay_track`` 按订单读取当前真实状态，
    位置沿已下发路线按进度推进（无路线时按起讫点直线走廊推进）；
  * ``inject_fault_reading`` 仅为原型期 F1 仿真档使用：按故障谱把遥测置为异常读数，
    用于验证 detect_exception 规则引擎（生产环境无此调用）。

接入真实 IoT 平台时，只需把 report_telemetry 换成消息消费、数据源换成本地落盘文件。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.gps")
mcp = FastMCP("gps-mcp") if HAVE_MCP else _StubMCP()

_DB_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "gps_state.json"

# order_id -> 状态：route（已下发路线）、progress（0~1 推进进度）、
#               telemetry（最近一次 IoT 上报）、dwell_since（停留起始 ts）、history
_STATE: Dict[str, Dict[str, Any]] = {}


def _load() -> None:
    if _DB_FILE.is_file():
        try:
            _STATE.update(json.loads(_DB_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa
            logger.warning("[gps] 状态文件读取失败，从空状态开始：%s", exc)


def _save() -> None:
    try:
        _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DB_FILE.write_text(json.dumps(_STATE, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except Exception as exc:  # noqa
        logger.warning("[gps] 状态落盘失败：%s", exc)


_load()

# 城市经纬度复用 map-mcp 的内置路网（同一份真实数据源，避免两套口径）
from sca.mcp.map_server import _CITY_DB, _norm_city  # noqa: E402

# 故障码 -> 仿真异常遥测读数（原型期 F1 档用；生产读数来自真实设备）
_FAULT_READINGS: Dict[str, Dict[str, Any]] = {
    "E-COLD-OVER":  {"temp_c": 11.8, "door_closed": True},
    "E-COLD-FAIL":  {"temp_c": 16.4, "door_closed": True},
    "E-COLD-DRIFT": {"temp_c": 7.9, "door_closed": True},
    "E-DG-OVER":    {"temp_c": 38.5, "door_closed": True},
    "E-COLD-DOOR":  {"door_closed": False},
    "E-CAP-BREAK":  {"speed": 0},
    "E-TMS-STOP":   {"speed": 0},
    "E-JAM":        {"speed": 12},
    "E-ROAD-CLOSED": {"speed": 0},
}


def _default_telemetry(order_id: str) -> Dict[str, Any]:
    return {"order_id": order_id, "temp_c": 3.2, "door_closed": True,
            "fuel_pct": 0.68, "speed": 72, "ts": int(time.time())}


def _lerp_position(order_id: str) -> Dict[str, Any]:
    """当前位置：沿已下发路线按进度插值。

    默认进度为 0 —— **司机实时位置与起运地一致**：异常刚上报、运单刚下发时没有真实
    GPS 依据，不编造"已走了 xx% 行程"的坐标（2026-10-15 用户要求）。只有 report_telemetry
    真的带经纬度上报过，或显式把 progress 推上去（仿真档 / replay_track），才会有位移。
    """
    st = _STATE.setdefault(order_id, {})
    prog = float(st.get("progress") or 0.0)
    tel = st.get("telemetry") or {}
    speed = float(tel.get("speed", 72))
    route = st.get("route") or {}
    # 途经点与坐标成对保留：单独过滤坐标会让下标与城市名错位（老实现的城市名为近似值）
    pairs = [(str(w).strip(), _CITY_DB.get(_norm_city(w)))
             for w in (route.get("waypoints") or [])]
    pairs = [(w, c) for w, c in pairs if c]
    if pairs:
        coords = [c for _, c in pairs]
        names = [w for w, _ in pairs]
        total = len(coords) - 1
        pos = min(max(prog, 0.0), 0.999) * total
        i = min(int(pos), total - 1) if total >= 1 else 0
        f = pos - i if total >= 1 else 0.0
        (x1, y1), (x2, y2) = coords[i], (coords[i + 1] if total >= 1 else coords[0])
        return {"order_id": order_id, "city": names[i] if i < len(names) else names[-1],
                "lng": round(x1 + (x2 - x1) * f, 4), "lat": round(y1 + (y2 - y1) * f, 4),
                "speed": speed, "ts": int(time.time()),
                "progress": round(prog, 4), "source": "route",
                # 起运地（进度 0）标记：前端据此说明"尚未开始移动"
                "at_origin": prog <= 0.0}
    # 无路线但有真实上报坐标：用真实定位点，不编造
    fix = st.get("last_fix") or {}
    if fix.get("lng") is not None and fix.get("lat") is not None:
        return {"order_id": order_id, "city": None,
                "lng": fix["lng"], "lat": fix["lat"], "speed": speed,
                "ts": int(time.time()), "progress": round(prog, 4),
                "source": "reported", "at_origin": False}
    # 既无路线也无上报：如实说位置未知，不返回硬编码坐标
    return {"order_id": order_id, "city": None, "lng": None, "lat": None,
            "speed": speed, "ts": int(time.time()), "progress": 0.0,
            "source": "unknown", "at_origin": True, "no_route": True,
            "note": "未下发路线且无 GPS 上报，位置未知（不编造坐标）"}


# 是否按采样间隔自动推进行程进度（原型期在途仿真档）。
# **默认关闭**：异常描述驱动的流程里没有真实 GPS 上报，"司机实时位置"就应等于起运地。
# 需要演示在途推进（位置沿路线前进）时置 SCA_GPS_AUTO_ADVANCE=1。
_AUTO_ADVANCE = os.environ.get("SCA_GPS_AUTO_ADVANCE", "0").strip().lower() \
    not in ("0", "", "false", "no")


def _advance(order_id: str, hours: float = 0.6) -> None:
    """推进一次采样间隔的行程进度（仅 SCA_GPS_AUTO_ADVANCE=1 的仿真档启用）。"""
    st = _STATE.setdefault(order_id, {})
    tel = st.get("telemetry") or {}
    speed = float(tel.get("speed", 72))
    route = st.get("route") or {}
    dist = float(route.get("distance_km") or 0)
    if _AUTO_ADVANCE and dist > 0 and speed > 5:
        st["progress"] = min(1.0, float(st.get("progress") or 0.0) + speed * hours / dist)
    # 停留统计
    if speed < 5:
        if not st.get("dwell_since"):
            st["dwell_since"] = time.time()
    else:
        st["dwell_since"] = None


@mcp.tool()
async def set_route(order_id: str, origin: str, dest: str,
                    waypoints: List[str] | None = None, distance_km: float = 0) -> dict:
    """路线下发：规划确定后把计划路线同步给车载追踪（生产对应 TMS→终端下发）。"""
    st = _STATE.setdefault(order_id, {})
    st["route"] = {"origin": origin, "dest": dest,
                   "waypoints": waypoints or [origin, dest], "distance_km": distance_km}
    st["progress"] = st.get("progress") or 0.0
    _save()
    return {"ok": True, "order_id": order_id, "route": st["route"]}


@mcp.tool()
async def report_telemetry(order_id: str, temp_c: float | None = None,
                           door_closed: bool | None = None, fuel_pct: float | None = None,
                           speed: float | None = None,
                           lng: float | None = None, lat: float | None = None) -> dict:
    """IoT 遥测上报入口（车载终端/北斗网关调用），覆盖合并后落盘。"""
    st = _STATE.setdefault(order_id, {})
    tel = st.get("telemetry") or _default_telemetry(order_id)
    for k, v in (("temp_c", temp_c), ("door_closed", door_closed),
                 ("fuel_pct", fuel_pct), ("speed", speed)):
        if v is not None:
            tel[k] = v
    tel["ts"] = int(time.time())
    st["telemetry"] = tel
    if lng is not None and lat is not None:
        st.setdefault("last_fix", {"lng": lng, "lat": lat, "ts": tel["ts"]})
        st["last_fix"].update({"lng": lng, "lat": lat, "ts": tel["ts"]})
    _save()
    return {"ok": True, "order_id": order_id, "telemetry": tel}


@mcp.tool()
async def get_latest_position(order_id: str) -> dict:
    """获取运单最新 GPS 位置与速度。

    默认（SCA_GPS_AUTO_ADVANCE 未开）位置即已下发路线的**起运地**——司机尚未移动，
    不编造行程进度；返回 at_origin / city 供前端如实展示。
    """
    _advance(order_id)
    pos = _lerp_position(order_id)
    if (pos.get("speed") or 0) < 5:
        st = _STATE.get(order_id) or {}
        if st.get("dwell_since"):
            pos["dwell_min"] = round((time.time() - st["dwell_since"]) / 60, 1)
    _save()
    return pos


@mcp.tool()
async def get_telemetry(order_id: str) -> dict:
    """获取车载 IoT 遥测（温控/车门/油量/车速），含停留时长。"""
    st = _STATE.get(order_id)
    tel = dict((st or {}).get("telemetry") or _default_telemetry(order_id))
    if st and st.get("dwell_since"):
        tel["dwell_min"] = round((time.time() - st["dwell_since"]) / 60, 1)
    return tel


@mcp.tool()
async def replay_track(order_id: str, steps: int = 5) -> list:
    """回放最近 N 个轨迹点（沿已下发路线按进度回溯，用于偏航检测与 ETA 估算）。"""
    st = _STATE.get(order_id) or {}
    base = float(st.get("progress") or 0.0)
    route = st.get("route") or {}
    dist = float(route.get("distance_km") or 0)
    pts: List[Dict[str, Any]] = []
    for i in range(steps):
        st["progress"] = max(0.0, base - i * 0.02) if dist else base
        p = _lerp_position(order_id)
        p["seq"] = i
        pts.append(p)
    st["progress"] = base
    _save()
    return pts


@mcp.tool()
async def inject_fault_reading(order_id: str, code: str) -> dict:
    """【仅原型期 F1 仿真】按故障谱把该订单遥测置为异常读数，供规则引擎验证。"""
    reading = _FAULT_READINGS.get(code)
    if not reading:
        return {"ok": False, "error": f"故障码 {code} 无仿真遥测映射"}
    await report_telemetry(order_id, **reading)
    return {"ok": True, "order_id": order_id, "code": code,
            "telemetry": _STATE[order_id]["telemetry"]}


@mcp.tool()
async def distance_to_route(order_id: str, lng: float, lat: float) -> dict:
    """计算坐标点到已下发路线走廊的垂直距离（km，偏航检测用）。"""
    st = _STATE.get(order_id) or {}
    route = st.get("route") or {}
    wps = [_norm_city(w) for w in (route.get("waypoints") or [])]
    coords = [_CITY_DB[w] for w in wps if w and w in _CITY_DB]
    if len(coords) < 2:
        return {"ok": False, "error": "该订单未下发路线"}
    best = min(_point_to_segment_km(lng, lat, coords[i], coords[i + 1])
               for i in range(len(coords) - 1))
    return {"ok": True, "order_id": order_id, "off_route_km": round(best, 2)}


def _point_to_segment_km(lng: float, lat: float, a: tuple, b: tuple) -> float:
    """点到线段最近距离（等距圆柱近似，城市尺度足够精确）。"""
    ax, ay = math.radians(a[0]), math.radians(a[1])
    bx, by = math.radians(b[0]), math.radians(b[1])
    px, py = math.radians(lng), math.radians(lat)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    # 大圆距离（km）
    dlat, dlng = py - cy, px - cx
    h = math.sin(dlat / 2) ** 2 + math.cos(cy) * math.cos(py) * math.sin(dlng / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
