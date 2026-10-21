"""MCP 工具层（FR-11 / FR-03）：地图 / 路径规划 Server。

原型期 F1 档位：使用内置真实城市经纬度路网做路径计算（非随机数硬编码）：
  * 城市解析支持别名（沪/京/穗/蓉…）与"省/市"后缀容错；
  * 里程 = 大圆距离(haversine) × 公路绕行系数，途经点按"最小绕行"原则在真实
    枢纽城市中选取，而非固定模板；
  * ETA = 里程 / 车型均速 + 强制休息（每 4h 休息 0.5h）+ 装卸时长；
  * 成本 = 里程 × 单价 + 起步价 + 温控/危品附加。
配置了 SCA_AMAP_KEY 时自动切换高德真实路径规划（失败自动降级内置路网）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP
from sca.mcp.route_llm import RoutePlannerLLM, RouteConstraints

logger = logging.getLogger("sca.mcp.map")


def _amap_key() -> str:
    """高德 Key：优先取「工具 Key 清单」中当前可用的一条，回退 settings/环境变量。"""
    try:
        from sca.credentials import first_credential
        c = first_credential("amap")
        k = ((c or {}).get("data") or {}).get("key", "").strip() if c else ""
        if k:
            return k
    except Exception:  # noqa: 清单不可用 -> 走原单点逻辑
        pass
    try:
        from sca.settings import settings
        return (getattr(settings, "amap_key", "") or "").strip() \
            or os.environ.get("SCA_AMAP_KEY", "").strip()
    except Exception:
        return os.environ.get("SCA_AMAP_KEY", "").strip()
mcp = FastMCP("map-mcp") if HAVE_MCP else _StubMCP()

# ---------------------------------------------------------------------------
# 真实城市经纬度（主要干线城市，用于里程/途经点计算）
# ---------------------------------------------------------------------------
_CITY_DB_MAJOR: Dict[str, Tuple[float, float]] = {
    "北京": (116.41, 39.90), "上海": (121.47, 31.23), "广州": (113.26, 23.13),
    "深圳": (114.06, 22.55), "成都": (104.07, 30.57), "重庆": (106.55, 29.56),
    "杭州": (120.15, 30.27), "南京": (118.80, 32.06), "武汉": (114.31, 30.52),
    "西安": (108.94, 34.34), "郑州": (113.63, 34.75), "长沙": (112.94, 28.23),
    "青岛": (120.38, 36.07), "天津": (117.20, 39.13), "沈阳": (123.43, 41.80),
    "哈尔滨": (126.53, 45.80), "长春": (125.32, 43.82), "昆明": (102.71, 25.04),
    "贵阳": (106.63, 26.65), "南宁": (108.37, 22.82), "福州": (119.30, 26.08),
    "厦门": (118.09, 24.48), "合肥": (117.28, 31.86), "济南": (117.00, 36.65),
    "太原": (112.55, 37.87), "石家庄": (114.51, 38.04), "兰州": (103.83, 36.06),
    "乌鲁木齐": (87.62, 43.82), "呼和浩特": (111.75, 40.84), "南昌": (115.89, 28.68),
    "海口": (110.20, 20.04), "苏州": (120.58, 31.30), "宁波": (121.55, 29.87),
    "无锡": (120.30, 31.57), "东莞": (113.75, 23.04), "佛山": (113.12, 23.02),
    "徐州": (117.18, 34.26), "洛阳": (112.45, 34.62), "襄阳": (112.14, 32.04),
    "宜昌": (111.29, 30.69), "赣州": (114.93, 25.83), "株洲": (113.13, 27.83),
    "韶关": (113.59, 24.81), "遵义": (106.93, 27.73), "柳州": (109.42, 24.33),
    # 省份映射所需的省会/直辖市补充（避免 _haversine KeyError）
    "拉萨": (91.11, 29.65), "西宁": (101.78, 36.62), "银川": (106.23, 38.49),
    "台北": (121.56, 25.03), "香港": (114.17, 22.32), "澳门": (113.55, 22.20),
}

# ---------------------------------------------------------------------------
# 全国城市库（离线）：scripts/fetch_cities.py 抓取的省/市/区县中心点经纬度。
# 合规：仅使用行政区划"中心点经纬度"（地理事实）做本地里程估算，不渲染地图、
# 不调用任何在线地图服务、不涉国界/界线绘制；数据源为阿里 DataV 行政区划
# （基于国家地理信息公共服务平台·天地图 标准地图）。
# ---------------------------------------------------------------------------
# 省份名（含/不含 省/自治区 后缀）-> 省会代表城市。
# 调度需求常只给到省份级（如"河北""广西→云南"），按省会代表城市做路径计算，
# 里程/ETA 真实可算；返回会带 degraded_reason 提示"已按省会估算，建议补录具体城市"。
_PROVINCE_CAPITAL: Dict[str, str] = {
    "河北": "石家庄", "山西": "太原", "辽宁": "沈阳", "吉林": "长春",
    "黑龙江": "哈尔滨", "江苏": "南京", "浙江": "杭州", "安徽": "合肥",
    "福建": "福州", "江西": "南昌", "山东": "济南", "河南": "郑州",
    "湖北": "武汉", "湖南": "长沙", "广东": "广州", "海南": "海口",
    "四川": "成都", "贵州": "贵阳", "云南": "昆明", "陕西": "西安",
    "甘肃": "兰州", "青海": "西宁", "台湾": "台北", "广西": "南宁",
    "内蒙古": "呼和浩特", "宁夏": "银川", "新疆": "乌鲁木齐", "西藏": "拉萨",
    "香港": "香港", "澳门": "澳门",
    "北京": "北京", "天津": "天津", "上海": "上海", "重庆": "重庆",
}

# 行政区划后缀（用于剥离"河北省/广西壮族自治区/北京市"等）
_REGION_SUFFIXES = ("特别行政区", "壮族自治区", "回族自治区", "维吾尔自治区",
                    "自治区", "省", "市", "地区", "自治州")


def _load_extra_cities() -> Dict[str, Tuple[float, float]]:
    """加载离线全国城市库（scripts/fetch_cities.py 抓取的行政区划中心点）。

    **必须剔除省级条目**：数据源里同时含省/自治区/直辖市本身（如
    `"河北":[114.5025,38.0455]`，坐标即石家庄）。若不剔除，"河北到南京"会被
    `_cities()` 当成"城市 河北"命中并排在同位置的省会映射之前，起讫地直接错成
    "河北→石家庄"，南京被整段丢弃（2026-10-15 实测复现）。省级语义统一由
    `_PROVINCE_CAPITAL` 的"省→省会"映射承担。
    """
    fp = Path(__file__).with_name("china_cities.json")
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # 缺文件不致命：退回仅内置城市
        return {}
    out: Dict[str, Tuple[float, float]] = {}
    for k, v in raw.items():
        k = str(k).strip()
        if len(k) < 2:      # 过滤单字名，避免"南"截胡"南宁"这类误匹配
            continue
        if k in _PROVINCE_CAPITAL:   # 省级条目不是城市：交给省→省会映射处理
            continue
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                out[k] = (float(v[0]), float(v[1]))
            except (TypeError, ValueError):
                continue
    return out


_EXTRA_CITIES: Dict[str, Tuple[float, float]] = _load_extra_cities()
# 合并：手工精挑的城市坐标优先（更准），扩展库补齐其余城镇
_CITY_DB: Dict[str, Tuple[float, float]] = {**_EXTRA_CITIES, **_CITY_DB_MAJOR}
# 子串匹配用（长名优先，避免短名误命中）
_CITY_BY_LEN: List[str] = sorted(_CITY_DB, key=len, reverse=True)

# 城市别名（货运口语常用）
_ALIAS: Dict[str, str] = {
    "沪": "上海", "京": "北京", "穗": "广州", "深": "深圳", "蓉": "成都",
    "渝": "重庆", "杭": "杭州", "宁": "南京", "汉": "武汉", "长安": "西安",
    "羊城": "广州", "鹏城": "深圳", "冰城": "哈尔滨", "春城": "昆明",
}

# 公路绕行系数（直线距离 -> 实际公路里程）
_ROAD_FACTOR = 1.25
# 车型均速（km/h）与附加时长（h）
_MODE_SPEED = {"truck": 65.0, "cold": 60.0, "hazmat": 55.0, "express": 70.0}
_REST_PER_HOURS = 4.0     # 每行驶 4 小时
_REST_HOURS = 0.5         # 休息时长
_LOAD_HOURS = 1.0         # 装卸时长

# 模型改道的「绕行接受阈值」：模型提议的绕行幅度超过基线的该比例即视为不可信，
# 回退原走廊并转人工确认。背景（2026-10-15 实测）：模型把高德真实途经的县级节点
# 误判为"非高速低等级路段"全部剔除，逼出「石家庄→郑州→南京」走廊（+67%），
# 对一次"避让封路"而言明显失真——模型只应被允许**局部绕行**，不应重画走廊。
_DETOUR_REJECT_RATIO = 0.35

# 途经点（县级）采样口径（2026-10-15 用户要求「路线结果要具体到县级」）：
# 沿高德 polyline 每 ~25km 取一点做地图匹配——东部干线上的县级节点间距多为 30-50km，
# 25km 能连续覆盖而不至于把每个乡镇都列进去；上限 _VIA_MAX 兼顾前端可读性与
# 高德 waypoints 参数（≤16 途经点，超出部分由 _amap_plan_with_key_via 均匀抽样）。
_VIA_SAMPLE_KM = 25.0
_VIA_MAX = 24

# 「受影响路段」的显式识别：调度员可直接录入（如"微山湖特大桥""G2 K620"），
# 也可写在需求原文里。只有**具体路段/地点**才能让模型做有依据的局部绕行；
# 只给异常类型（如"道路封闭"）时模型必然 accept，白白多花 15s（2026-10-15）。
_SEGMENT_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9]{0,8}(?:高速|国道|省道|快速路|干线|大桥|特大桥|隧道|"
    r"收费站|服务区|立交|互通|环线|枢纽|路段|方向|段)[\u4e00-\u9fa5A-Za-z0-9]{0,8}"
    r"|[Kk]\s?\d{2,4}(?:\s*[+\-]\s?\d{1,4})?")


def segment_hint(*texts: Optional[str]) -> str:
    """从若干候选文本里提取「具体受影响路段」描述；识别不出则返回空串。

    只认显式路况地名（高速/国道/大桥/隧道/服务区/桩号，或明确点名的城市），
    避免把"道路封闭""运输受阻"这类**泛化异常描述**误当成可定位的路段——
    后者交给模型只会得到 accept（无依据可改），属纯损耗。
    """
    for t in texts:
        s = str(t or "").strip()
        if not s:
            continue
        m = _SEGMENT_RE.search(s)
        if m:
            return m.group(0).strip()
    return ""


def _norm_city(name: str, province_hint: bool = True) -> Optional[str]:
    """解析城市名：去后缀、别名映射、省份映射省会、模糊包含匹配。

    province_hint=False 时不做省份→省会映射（仅严格匹配已建路网城市）。
    """
    if not name:
        return None
    s = str(name).strip()
    # 剥离行政区划后缀（河北省/广西壮族自治区/北京市/XX特别行政区…）
    for suf in _REGION_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)].strip()
    if not s:
        return None
    if s in _ALIAS:
        return _ALIAS[s]
    # 省份名（无后缀，如"河北""广西""云南"）-> 省会代表城市（优先于省市同名歧义）
    if province_hint and s in _PROVINCE_CAPITAL:
        return _PROVINCE_CAPITAL[s]
    if s in _CITY_DB:
        return s
    # 包含匹配：如"广州市天河区" -> 广州；"沪昆高速" 无法匹配，返回 None。
    # 按名字长度降序，长名优先，避免短名误命中（如"海"类短名截胡"北海"）
    for city in _CITY_BY_LEN:
        if city in s:
            return city
    return None


def _haversine(a: str, b: str) -> float:
    """两城市大圆距离（km）。"""
    (lng1, lat1), (lng2, lat2) = _CITY_DB[a], _CITY_DB[b]
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _road_km(a: str, b: str) -> float:
    return round(_haversine(a, b) * _ROAD_FACTOR, 1)


def _km_coords(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两个 (lng, lat) 坐标点之间的距离（km）。

    与 `_haversine` 的区别：那个吃城市名、查 `_CITY_DB`；这个吃原始坐标，
    用于判断高德返回的路径采样点是否落在某个城市附近（改道避让验证）。
    """
    (lng1, lat1), (lng2, lat2) = a, b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _pick_hubs(origin: str, dest: str, max_hubs: int | None = None,
               detour_tol: float = 0.25, min_gap_km: float = 80.0) -> List[str]:
    """按最小绕行原则选取途经枢纽（县级细化版）。

    候选池由 _CITY_DB（含 china_cities.json 的县级名 2000+ 条）提供，不再局限于省会。
    枢纽个数按里程自适应（`_CITY_DB` 里绝大多数是县级条目，2000+ 条覆盖了走廊上的
    县级节点），并用 `min_gap_km` 做**最小间隔**约束，避免同一城市群里的几个邻县
    挤占全部名额（老实现固定取 4 个，长干线只有省会级粒度——2026-10-15 用户反馈
    「太宽泛、没有实际效果」）。同绕行比下优先"主要枢纽"（_CITY_DB_MAJOR）。
    """
    base = _haversine(origin, dest)
    if base <= 0:
        return []
    if max_hubs is None:
        max_hubs = max(3, min(10, int(base / 150) + 2))
    cand = []
    for city in _CITY_DB:
        if city in (origin, dest):
            continue
        d_o = _haversine(origin, city)
        d_d = _haversine(city, dest)
        ratio = (d_o + d_d) / base
        if ratio > 1.0 + detour_tol:
            continue
        # 偏好主要枢纽：非枢纽城市加 0.04 绕行惩罚，使省会/重点地级市在并列时胜出
        major_penalty = 0.0 if city in _CITY_DB_MAJOR else 0.04
        cand.append((round(ratio + major_penalty, 4), d_o, city))
    cand.sort()  # 绕行比最小优先，其次离起点近
    # 贪心取点：距起讫与已选枢纽都要 >= min_gap_km，保证沿走廊均匀铺开
    hubs: List[str] = []
    for _, d_o, city in cand:
        if d_o < min_gap_km or (base - d_o) < min_gap_km:
            continue
        if any(_haversine(city, h) < min_gap_km for h in hubs):
            continue
        hubs.append(city)
        if len(hubs) >= max_hubs:
            break
    # 途经点按"离起点由近到远"排序，保证是顺路
    return sorted(hubs, key=lambda c: _haversine(origin, c))


def _nearest_city(lng: float, lat: float) -> Optional[str]:
    """在 _CITY_DB 中找坐标最近的城市（平方距离近似，够用于地图匹配）。"""
    best, best_d = None, 1e18
    for city, (cl, ca) in _CITY_DB.items():
        dd = (cl - lng) ** 2 + (ca - lat) ** 2
        if dd < best_d:
            best_d, best = dd, city
    return best


def _step_sample_points(step: Dict[str, Any]) -> List[Tuple[float, float]]:
    """按"里程间隔"从单段 polyline 采样坐标点（含段首/段尾）。

    为什么要按里程而不是"每段取一个点"（2026-10-15 修）：高德在长直高速上会把整条
    高速合成**一个 step**，老实现只取该 step 的首点 -> 石家庄→南京 中间出现
    「济南 →（空 230km）→ 微山」这种断裂，途经城市层级忽县忽市、不均匀。
    这里按 step 自带的 distance（米）每 ~`_VIA_SAMPLE_KM` 取一个点，保证走廊上
    的县级节点被连续覆盖；distance 缺失时退化为按点数每 30 点取一个。
    """
    pts: List[Tuple[float, float]] = []
    for seg in str(step.get("polyline") or "").split(";"):
        parts = seg.split(",")
        if len(parts) == 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if not pts:
        return []
    dist_km = float(step.get("distance") or 0) / 1000.0
    n = (max(1, int(round(dist_km / _VIA_SAMPLE_KM))) if dist_km > 0
         else max(1, int(round(len(pts) / 30))))
    if len(pts) <= n:
        return pts
    idx = sorted({int(round(i * (len(pts) - 1) / n)) for i in range(n + 1)})
    return [pts[i] for i in idx]


# 只有"省份本身"才需要在地图匹配结果里剔除（直辖市如北京/上海既是省也是市，
# 它们是合法城市，不能被剔掉）
_PROVINCE_NAMES: set = {k for k, v in _PROVINCE_CAPITAL.items() if k != v}


def _extract_via_cities(steps: List[Dict[str, Any]], origin: str, dest: str,
                        max_via: int = _VIA_MAX) -> List[str]:
    """从高德 steps 的 polyline 采样点，地图匹配到最近城市，得到真实途经序列。

    采样按里程间隔（`_step_sample_points`），匹配后去重、保序、上限 max_via。
    返回"中间途经城市"（不含起讫），调用方负责拼成完整 waypoints。
    """
    pts: List[Tuple[float, float]] = []
    for s in steps:
        pts.extend(_step_sample_points(s))
    if not pts:
        return []
    cities: List[str] = []
    last = None
    for lng, lat in pts:
        c = _nearest_city(lng, lat)
        # 跳过省名（地图匹配偶发命中省级条目）与起讫
        if c and c != last and c not in (origin, dest) and c not in _PROVINCE_NAMES:
            cities.append(c)
            last = c
    # 过长则均匀抽样（保留首尾趋势）
    if len(cities) > max_via:
        stride = len(cities) / max_via
        cities = [cities[int(i * stride)] for i in range(max_via)]
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for c in cities:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _eta_hours(distance_km: float, mode: str = "truck") -> float:
    speed = _MODE_SPEED.get(mode, _MODE_SPEED["truck"])
    drive = distance_km / speed if speed else 0.0
    rest = (drive // _REST_PER_HOURS) * _REST_HOURS
    return round(drive + rest + _LOAD_HOURS, 1)


def _eta_final(distance_km: float, mode: str, amap_eta_h: float | None = None) -> float:
    """统一 ETA 口径（2026-10-15 修）。

    高德返回的 duration 是**轿车**路况时长（干线可到 80-90km/h），而货车/危品/冷链
    受车型均速与强制休息约束。直接采用会出现两个问题：① 口径断裂——同一次重规划里
    里程只涨 2.7%、ETA 却从 12.6h 跳到 16.9h（前值来自 8 折系数、后值来自高德轿车
    时长）；② 系统性低估——危品 1000km 报 12.6h 明显违反实际，会让 SLA/时效判定
    误判"能赶上"。这里取「高德时长」与「按车型均速 + 强制休息 + 装卸」两者的较大值，
    保证任何 provider 下口径一致且不低估。
    """
    by_mode = _eta_hours(distance_km, mode)
    if amap_eta_h is None:
        return by_mode
    return round(max(float(amap_eta_h), by_mode), 1)


def _cost(distance_km: float, cost_per_km: float = 2.6,
          temp_ctrl: bool = False, hazmat: bool = False) -> float:
    """运输成本：里程单价 + 起步价 + 温控/危品附加。"""
    base = distance_km * cost_per_km + 300.0
    if temp_ctrl:
        base += distance_km * 0.35
    if hazmat:
        base += distance_km * 0.55 + 200.0
    return round(base, 1)


async def _amap_plan_with_key(origin: str, dest: str, key: str) -> Optional[Dict[str, Any]]:
    """用指定 Key 调高德真实路径规划；失败抛异常或返回 None（供凭证链判定降级）。"""
    if not key:
        return None
    import json
    import urllib.parse
    import urllib.request

    o, d = _CITY_DB[origin], _CITY_DB[dest]
    q = urllib.parse.urlencode({"key": key, "origin": f"{o[0]},{o[1]}",
                                "destination": f"{d[0]},{d[1]}", "extensions": "all"})
    try:
        from sca.util.aio import fetch_bytes
        # fetch_bytes 把同步 urlopen 挪进线程：否则本协程不挂起，agent_route 与
        # agent_precheck 的并行扇出会退化成串行（并卡死整个 uvicorn 事件循环）
        j = json.loads((await fetch_bytes(
            f"https://restapi.amap.com/v3/direction/driving?{q}", timeout=6)).decode("utf-8"))
        paths = (j.get("route") or {}).get("paths") or []
        if not paths:
            return None
        path = paths[0]
        dist = float(path.get("distance", 0)) / 1000.0
        dur = float(path.get("duration", 0)) / 3600.0
        steps = path.get("steps", []) or []
        roads = [s.get("road") for s in steps if s.get("road")]
        # 解析 polyline -> 真实途经城市（地图匹配最近城市，去重保序）
        via_cities = _extract_via_cities(steps, origin, dest)
        return {"distance_km": round(dist, 1), "eta_h": round(dur, 1),
                "real_roads": roads[:12], "via_cities": via_cities,
                "provider": "amap"}
    except Exception as exc:
        logger.warning("[map] 高德路径规划失败（Key …%s）：%s", key[-4:], exc)
        raise  # 抛给凭证链：自动切换下一条 Key


async def _amap_plan(origin: str, dest: str) -> Optional[Dict[str, Any]]:
    """接高德真实路径规划：按「工具 Key 清单」依次尝试，失败自动降级下一条 Key。

    全部 Key 失败返回 None，由调用方降级内置路网（不编造里程）。
    """
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            k = (cred.get("data") or {}).get("key", "").strip()
            return await _amap_plan_with_key(origin, dest, k)

        real, used, attempts = await run_chain(KIND_TOOL, "amap", _one)
    except Exception as exc:  # noqa: 凭证链不可用 -> 单点回退
        logger.warning("[map] 凭证链不可用（%s），回退单 Key 调用", exc)
        return await _amap_plan_with_key(origin, dest, _amap_key())
    if real and used:
        real["credential"] = used.get("name", "")
    return real


async def _amap_plan_with_key_via(waypoints: List[str], key: str,
                                  with_points: bool = False) -> Optional[Dict[str, Any]]:
    """按**自定义途经点序列**调高德真实路径规划（v3 driving 的 waypoints 参数）。

    为什么需要：模型改道（换枢纽/规避城市）后若仍用 haversine×系数 估算里程，会
    出现「高德起、几何收」的口径断裂——同一份 plan 里途经点是真实的、里程却是估的
    （2026-10-15 实测：模型改走郑州后里程被几何算成 1369km，而真实高德仅约 1180km）。
    这里用真实途经点重新问一次高德，里程/ETA 全程同源。
    """
    if not key:
        return None
    import json
    import urllib.parse
    import urllib.request

    pts = [w for w in (waypoints or []) if w in _CITY_DB]
    if len(pts) < 2:
        return None
    o, d = _CITY_DB[pts[0]], _CITY_DB[pts[-1]]
    mid = pts[1:-1]               # 高德 waypoints 上限 16 个途经点
    if len(mid) > 16:             # 县级途经点可能超过 16：均匀抽样（而非截断前 16 个，
        stride = len(mid) / 16    # 否则会把后半程途经点整段丢掉）
        mid = [mid[int(i * stride)] for i in range(16)]
    q = {"key": key, "origin": f"{o[0]},{o[1]}", "destination": f"{d[0]},{d[1]}",
         "extensions": "all"}
    if mid:
        q["waypoints"] = ";".join(f"{_CITY_DB[w][0]},{_CITY_DB[w][1]}" for w in mid)
    try:
        from sca.util.aio import fetch_bytes
        # 同上：避免同步 urlopen 卡住事件循环
        j = json.loads((await fetch_bytes(
            f"https://restapi.amap.com/v3/direction/driving?{urllib.parse.urlencode(q)}",
            timeout=6)).decode("utf-8"))
        paths = (j.get("route") or {}).get("paths") or []
        if not paths:
            return None
        path = paths[0]
        out = {"distance_km": round(float(path.get("distance", 0)) / 1000.0, 1),
               "eta_h": round(float(path.get("duration", 0)) / 3600.0, 1),
               "provider": "amap"}
        if with_points:
            # 改道避让验证需要原始采样点：只剔除途经点**不等于**路径真的绕开了，
            # 必须回到几何上确认新路径没再穿过受影响城市（详见 plan_route_via）。
            pts_all: List[Tuple[float, float]] = []
            for st in (path.get("steps") or []):
                pts_all.extend(_step_sample_points(st))
            out["sample_points"] = pts_all
        return out
    except Exception as exc:
        logger.warning("[map] 高德途经点重算失败（Key …%s）：%s", key[-4:], exc)
        return None


async def _amap_plan_via(waypoints: List[str],
                         with_points: bool = False) -> Optional[Dict[str, Any]]:
    """走凭证链的途经点式真实重算；失败返回 None（调用方降级几何估算并标注）。"""
    try:
        from sca.credentials import KIND_TOOL, run_chain

        async def _one(cred):
            k = (cred.get("data") or {}).get("key", "").strip()
            return await _amap_plan_with_key_via(waypoints, k, with_points=with_points)

        real, used, _ = await run_chain(KIND_TOOL, "amap", _one)
    except Exception as exc:  # noqa: 凭证链不可用 -> 单点回退
        logger.warning("[map] 途经点重算凭证链不可用（%s）", exc)
        return await _amap_plan_with_key_via(waypoints, _amap_key(),
                                             with_points=with_points)
    if real and used:
        real["credential"] = used.get("name", "")
    return real


@mcp.tool()
async def plan_route(origin: str, dest: str, avoid: str | None = None,
                     cargo_type: str = "普货", temp_ctrl: bool = False,
                     hazmat: bool = False, cost_per_km: float = 2.6,
                     mode: str | None = None) -> dict:
    """路径规划：返回真实途径点 / 里程 / ETA / 成本；avoid 指定需避开的路段/城市。"""
    o_raw, d_raw = str(origin).strip(), str(dest).strip()
    o, d = _norm_city(o_raw), _norm_city(d_raw)
    if not o or not d:
        return {"ok": False, "error": f"无法解析城市：{origin} / {dest}（内置路网含 {len(_CITY_DB)} 个主要城市）"}
    if o == d:
        return {"ok": False, "error": "起点与终点相同"}

    # 省份名 -> 省会映射提示（非编造，按货运惯例以省会为起讫代表城市）
    notes = []
    if o_raw != o and o_raw in _PROVINCE_CAPITAL:
        notes.append(f"起点按省会映射：{o_raw}→{o}")
    if d_raw != d and d_raw in _PROVINCE_CAPITAL:
        notes.append(f"终点按省会映射：{d_raw}→{d}")

    m = mode or ("cold" if temp_ctrl else ("hazmat" if hazmat else "truck"))
    hubs = _pick_hubs(o, d)
    geo_waypoints = [o] + hubs + [d]
    # 分段里程求和（比单纯直线更贴近真实路网）
    distance = round(sum(_road_km(geo_waypoints[i], geo_waypoints[i + 1])
                         for i in range(len(geo_waypoints) - 1)), 1)

    real = await _amap_plan(o, d) if _amap_key() else None
    credential_used = ""
    via: List[str] = []
    if real:
        distance = real["distance_km"]
        eta = _eta_final(distance, m, real["eta_h"])
        provider, roads = "amap", real["real_roads"]
        credential_used = real.get("credential", "")
        via = real.get("via_cities") or []
        # 真实途经城市优先覆盖几何枢纽（更贴近实际走廊）
        waypoints = [o] + via + [d] if via else geo_waypoints
    else:
        eta = _eta_hours(distance, m)
        provider, roads = "builtin-road-network", []
        waypoints = geo_waypoints

    # 避开路段：仅当能定位到具体路段时才计绕行。文本级描述无法映射到路段时不
    # 加成任何里程（原实现无条件 +8% 属凑数），改为注明并建议补录。
    detour_km = 0.0
    if avoid:
        notes.append("受影响路段为文本描述、未映射到具体路段：维持原走廊里程，"
                     "未计入绕行（避免编造），建议补录受影响路段或人工确认")

    return {
        "ok": True,
        "origin": o, "dest": d, "avoid": avoid or None,
        "waypoints": waypoints, "via_cities": via,
        "distance_km": distance,
        "detour_km": detour_km, "eta_h": eta,
        "mode": m, "provider": provider, "real_roads": roads,
        "credential": credential_used or None,  # 命中的工具 Key（多 Key 降级可追溯）
        "notes": notes,
        "cost": {"est": _cost(distance, cost_per_km, temp_ctrl, hazmat), "currency": "CNY"},
        "formula": {"road_factor": _ROAD_FACTOR, "speed_kmh": _MODE_SPEED.get(m),
                    "rest_h": round((distance / _MODE_SPEED.get(m, 65.0)) // _REST_PER_HOURS * _REST_HOURS, 1),
                    "load_h": _LOAD_HOURS},
    }


@mcp.tool()
async def plan_route_via(waypoints: list[str], cargo_type: str = "普货",
                         temp_ctrl: bool = False, hazmat: bool = False,
                         cost_per_km: float = 2.6,
                         mode: str | None = None,
                         avoid_city: str = "",
                         avoid_radius_km: float = 60.0) -> dict:
    """按**指定途经点序列**规划路径，返回真实里程与 ETA。

    与 `plan_route` 的区别：`plan_route` 自己选枢纽，这里由调用方指定途经点，
    用于应急改道——把受影响城市从途经点里剔除后重新问一次高德，得到的是
    **真实绕行里程**，不是按系数拍出来的。

    为什么还要 `avoid_city`：只把城市从**途经点**里删掉，并不能保证高德算出来的
    路径不经过它（郑州→栾川必经洛阳一带，删掉途经点也会路过）。所以这里回到几何上
    验证：把高德返回的路径采样点逐个比对，只要有采样点落在 `avoid_city` 的
    `avoid_radius_km` 半径内，就置 `avoid_hit=True`，调用方据此判定"没绕开"。
    没有这层验证，所谓改道就是自欺欺人。

    能力边界：高德 v3 driving 没有"避开某城市"参数，绕行只能靠改途经点实现，
    因此这里是**城市级绕行**，不是路段级避让，也不保证全局最优。
    高德不可用时退回几何估算，`provider=builtin-road-network`，此时无法验证避让，
    `avoid_hit=None`（未知，而不是"已避开"）。

    返回：{ok, waypoints, distance_km, eta_h, provider, cost, avoid_hit, notes}
    """
    pts = [str(w).strip() for w in (waypoints or []) if str(w or "").strip()]
    if len(pts) < 2:
        return {"ok": False, "error": "途经点至少需要起运地与目的地两个"}
    o, d = _norm_city(pts[0]), _norm_city(pts[-1])
    if not o or not d:
        return {"ok": False, "error": f"无法解析起讫城市：{pts[0]} / {pts[-1]}",
                "provider": "unavailable"}
    pts[0], pts[-1] = o, d
    m = mode or ("cold" if temp_ctrl else ("hazmat" if hazmat else "truck"))
    avoid_key = _norm_city(avoid_city) if avoid_city else ""

    notes: list[str] = []
    real = await _amap_plan_via(pts, with_points=bool(avoid_key)) if _amap_key() else None
    avoid_hit = None
    if real and real.get("distance_km"):
        distance = float(real["distance_km"])
        eta = _eta_final(distance, m, real.get("eta_h"))
        provider = "amap"
        notes.append("高德真实路径（按指定途经点重算）")
        sample = real.get("sample_points") or []
        target = _CITY_DB.get(avoid_key) if avoid_key else None
        if target and sample:
            hit = sum(1 for p in sample
                      if _km_coords(p, target) <= float(avoid_radius_km))
            avoid_hit = hit > 0
            notes.append(f"避让校验：{len(sample)} 个路径采样点中 {hit} 个落在"
                         f"{avoid_key} {avoid_radius_km:.0f}km 内")
    else:
        distance = round(sum(_road_km(pts[i], pts[i + 1])
                             for i in range(len(pts) - 1)), 1)
        eta = _eta_hours(distance, m)
        provider = "builtin-road-network"
        notes.append("高德不可达：按直线距离 × 公路系数估算，非真实路况；"
                     "无法验证是否真的绕开（avoid_hit 未知）")

    return {"ok": True, "waypoints": pts, "distance_km": distance, "eta_h": eta,
            "provider": provider, "mode": m, "avoid_city": avoid_key or None,
            "avoid_hit": avoid_hit,
            "cost": {"est": _cost(distance, cost_per_km, temp_ctrl, hazmat),
                     "currency": "CNY"},
            "notes": notes}


@mcp.tool()
async def plan_route_smart(origin: str, dest: str, cargo_type: str = "普货",
                           temp_ctrl: bool = False, hazmat: bool = False,
                           cost_per_km: float = 2.6, mode: str | None = None,
                           natural_lang_req: str = "", use_model: bool = False,
                           avoid: str | None = None, allow_model: bool = True) -> dict:
    """路径规划（模型增强版，FR-03+）：S1 需求结构化 → S2 模型约束提取 →
    S3 路线初算（几何+高德）→ S4 模型选路裁决 → S5 终算+解释。
    avoid 指定需避开的受影响路段/城市（应急重规划 replan 会传入），语义与
    `plan_route` 保持一致（绕行惩罚 + 标记 detour_km）。
    allow_model=False 时**跳过 S2/S4/S5 三次模型调用**（决策记 source=skipped）：
    应急重规划场景下若调度员没给具体受影响路段，模型对"某类异常影响本行程"无
    可依据的信息，实测必然输出 accept——白等 12-30s（2026-10-15 用户确认）。
    模型全程 fail-open：不可用则退回纯几何/高德初稿（向后兼容 plan_route 的返回结构）。"""
    o_raw, d_raw = str(origin).strip(), str(dest).strip()
    o, d = _norm_city(o_raw), _norm_city(d_raw)
    if not o or not d:
        return {"ok": False, "error": f"无法解析城市：{origin} / {dest}",
                "provider": "unavailable"}
    if o == d:
        return {"ok": False, "error": "起点与终点相同", "provider": "unavailable"}

    notes = []
    if o_raw != o and o_raw in _PROVINCE_CAPITAL:
        notes.append(f"起点按省会映射：{o_raw}→{o}")
    if d_raw != d and d_raw in _PROVINCE_CAPITAL:
        notes.append(f"终点按省会映射：{d_raw}→{d}")

    # S1 需求结构化（确定性，已在 _norm_city；base 约束）
    m = mode or ("cold" if temp_ctrl else ("hazmat" if hazmat else "truck"))

    # S2 模型约束提取（把 avoid 一并喂给模型，使其决策时规避受影响路段）
    llm = RoutePlannerLLM()
    req_text = str(natural_lang_req or "").strip()
    if avoid and avoid not in req_text:
        req_text = f"{req_text}；需避开：{avoid}".strip("；")
    want_model = allow_model and (bool(req_text) or use_model)
    if not allow_model:
        notes.append("未提供具体受影响路段（无高速/大桥/隧道/桩号等可定位信息）："
                     "跳过模型裁决，按确定性路线维持原走廊；如需局部绕行请补录受影响路段")
    constraints = await llm.extract_constraints(req_text) if want_model else RouteConstraints()

    # S3 路线初算（确定性 + 高德）
    hubs = _pick_hubs(o, d)
    geo_waypoints = [o] + hubs + [d]
    distance = round(sum(_road_km(geo_waypoints[i], geo_waypoints[i + 1])
                         for i in range(len(geo_waypoints) - 1)), 1)
    real = await _amap_plan(o, d) if _amap_key() else None
    via: List[str] = []
    # 无高德 Key / 高德失败时下面三个分支都不会赋值，若无此初始化则在函数末尾
    # 引用 `credential_used` 会抛 UnboundLocalError —— 表现为"没配高德 Key 时
    # plan_route_smart 整体失败并降级"，与"高德可用"路径行为不一致（2026-10-16 修）。
    credential_used = ""
    if real:
        distance = real["distance_km"]
        eta = _eta_final(distance, m, real["eta_h"])
        provider, roads = "amap", real["real_roads"]
        credential_used = real.get("credential", "")
        via = real.get("via_cities") or []
        # 真实途经城市优先覆盖几何枢纽（更贴近实际走廊）
        waypoints = [o] + via + [d] if via else geo_waypoints
    else:
        eta = _eta_hours(distance, m)
        provider, roads = "builtin-road-network", []
        waypoints = geo_waypoints

    candidate = {"waypoints": waypoints, "distance_km": distance, "eta_h": eta,
                 "provider": provider}

    # S4 模型选路裁决（原生 function calling：模型可真调 geocode/estimate_eta；
    # 模型不可用 -> accept 初稿，fail-open；allow_model=False -> 直接 accept 且不调用）
    if want_model:
        decision = await llm.adjudicate(candidate, constraints, cargo_type, temp_ctrl,
                                        hazmat, tool_executor=_route_tool_executor)
    else:
        decision = {"decision": "accept", "avoid_cities": [], "swap_hub": None,
                    "reasons": [], "risks": [], "source": "skipped",
                    "model_name": ""}
    # —— 基线快照（模型介入前的确定性初稿）：绕行闸口的比较基准 ——
    base_waypoints, base_distance, base_eta = list(waypoints), float(distance), float(eta)

    final_waypoints = list(waypoints)
    model_changed = False
    if decision.get("avoid_cities"):
        trimmed = [w for w in final_waypoints
                   if w not in decision["avoid_cities"] or w in (o, d)]
        if len(trimmed) >= 2:
            final_waypoints = trimmed
            notes.append(f"模型规避城市：{', '.join(decision['avoid_cities'])}")
            model_changed = True
    if decision.get("swap_hub") and decision["swap_hub"] in _CITY_DB:
        hub = decision["swap_hub"]
        if hub not in (o, d) and hub not in final_waypoints and len(final_waypoints) >= 3:
            final_waypoints[1] = hub
            notes.append(f"模型替换枢纽：{hub}")
            model_changed = True

    # 模型改道后重算里程：**优先用真实途经点再问一次高德**（口径同源），
    # 高德不可用才退回几何估算并显式标注 provider，绝不在 provider=amap 下混估数。
    if model_changed:
        rp = await _amap_plan_via(final_waypoints) if _amap_key() else None
        base_rp = (await _amap_plan_via(base_waypoints)
                   if (rp and rp.get("distance_km") and _amap_key()) else None)
        if rp and rp.get("distance_km") and base_rp and base_rp.get("distance_km"):
            # 里程必须**同口径**求"真实改道增量"（2026-10-15 实测修正）：
            # 高德的 waypoints 参数要求路线**逐个穿过**给定坐标，每穿过一个城市中心
            # 就要下高速再上来——途经点越密、这段"固有增量"越大。实测 石家庄→南京：
            #   直连 O→D            884.9km
            #   链式基线（25 途经点）1072.0km  <- 固有增量 +187km
            #   链式新走廊（避2城） 1183.5km
            # 若拿"链式新走廊"直接减"直连基线"，会把 +187km 的固有增量算成绕行
            # （虚报 +298.6km，逼近 35% 闸口，会让一次正确的局部避让被误判驳回）。
            # 两次都走链式口径相减 -> 只剩真实改道增量 +111.5km。
            chain_new = float(rp["distance_km"])
            chain_base = float(base_rp["distance_km"])
            detour_pure = max(0.0, round(chain_new - chain_base, 1))
            distance = round(base_distance + detour_pure, 1)
            eta = _eta_final(distance, m)
            provider = "amap(waypoints)"
            if rp.get("credential"):
                credential_used = rp["credential"]
            notes.append(
                f"改道增量按同口径（高德途经点链）求得：{chain_base}km → {chain_new}km，"
                f"真实绕行 +{detour_pure}km（链式口径含'逐个穿城'固有里程，故不与直连里程直接相减）；"
                f"展示里程 = 直连 {base_distance}km + 绕行 {detour_pure}km = {distance}km")
        elif rp and rp.get("distance_km"):
            # 拿不到链式基线（第二次调用失败）：退回链式新走廊里程，并如实标注口径
            distance = float(rp["distance_km"])
            eta = _eta_final(distance, m, rp["eta_h"])
            provider = "amap(waypoints)"
            if rp.get("credential"):
                credential_used = rp["credential"]
            notes.append("改道后里程取自高德途经点链（未能取得同口径基线，未做固有增量扣减）")
        else:
            distance = round(sum(_road_km(final_waypoints[i], final_waypoints[i + 1])
                                 for i in range(len(final_waypoints) - 1)), 1)
            eta = _eta_hours(distance, m)
            provider = "builtin-road-network"
            notes.append("模型改道后高德途经点重算不可用，里程按内置路网几何估算（已标注来源）")

    # —— 绕行接受闸口（确定性裁决，与长短期记忆子系统同一原则：模型只提案）——
    # 模型改道幅度超阈值即回退基线走廊：一次"避让封路"不应把走廊整体重画。
    detour_km, model_rejected = 0.0, False
    if model_changed:
        detour_km = round(max(0.0, distance - base_distance), 1)
        ratio = (detour_km / base_distance) if base_distance else 0.0
        if ratio > _DETOUR_REJECT_RATIO:
            notes.append(
                f"模型改道绕行 +{detour_km}km（+{ratio * 100:.0f}%）超出可接受阈值 "
                f"{int(_DETOUR_REJECT_RATIO * 100)}%，已回退原走廊 {base_distance}km，建议人工确认")
            final_waypoints, distance, eta = base_waypoints, base_distance, base_eta
            detour_km, model_rejected = 0.0, True
        else:
            notes.append(f"模型改道：{base_distance}km → {distance}km（绕行 +{detour_km}km）")
    elif avoid and allow_model:
        # 模型跑了但没改道（accept）：不做任何编造性绕行加成，维持原走廊里程并注明
        # （原实现无条件 +8% 属凑数，与"不编造"原则冲突）。allow_model=False 时
        # 上文的"跳过模型裁决"说明已覆盖同一意思，不重复提示。
        notes.append("受影响路段未映射到具体路段、模型未提出改道：维持原走廊里程，"
                     "未计入绕行（避免编造），建议补录受影响路段或人工确认")

    # S5 终算 + 解释（跳过模型时不给解释，留空由前端按 notes 呈现）
    cost = _cost(distance, cost_per_km, temp_ctrl, hazmat)
    summary = (await llm.summarize(
        {"waypoints": final_waypoints, "distance_km": distance, "eta_h": eta,
         "provider": provider}, constraints, decision)) if want_model else ""

    return {
        "ok": True,
        "origin": o, "dest": d,
        "waypoints": final_waypoints, "via_cities": via, "distance_km": distance,
        "detour_km": detour_km, "avoid": avoid or None, "eta_h": eta, "mode": m,
        "provider": provider, "real_roads": roads,
        "credential": credential_used or None,
        "model_applied": model_changed and not model_rejected,
        "model_rejected": model_rejected,
        "notes": notes,
        "cost": {"est": cost, "currency": "CNY"},
        "formula": {"road_factor": _ROAD_FACTOR,
                    "speed_kmh": _MODE_SPEED.get(m),
                    "rest_h": round((distance / _MODE_SPEED.get(m, 65.0)) // _REST_PER_HOURS * _REST_HOURS, 1),
                    "load_h": _LOAD_HOURS},
        # —— 模型增强产物（供前端/审计）——
        "model_constraints": constraints.as_dict(),
        "model_decision": {
            "decision": decision.get("decision"),
            "avoid_cities": decision.get("avoid_cities", []),
            "swap_hub": decision.get("swap_hub"),
            "reasons": decision.get("reasons", []),
            "risks": decision.get("risks", []),
            "source": decision.get("source"),
            "model_name": decision.get("model_name", ""),
        },
        "summary": summary,
        "smart": True,          # 走到的是 smart 编排（含 allow_model=False 的跳过档）
        "model_used": want_model,   # 是否真的调用了模型（False=按需跳过/无凭证）
    }


@mcp.tool()
async def estimate_eta(route: str | None = None, mode: str = "truck",
                       distance_km: float | None = None,
                       origin: str | None = None, dest: str | None = None) -> dict:
    """ETA 估算：支持按里程或按起讫城市。"""
    if distance_km is None and origin and dest:
        o, d = _norm_city(origin), _norm_city(dest)
        if o and d:
            distance_km = _road_km(o, d)
    if distance_km is None:
        return {"ok": False, "error": "需提供 distance_km 或 origin/dest"}
    eta = _eta_hours(float(distance_km), mode)
    return {"ok": True, "distance_km": float(distance_km), "mode": mode,
            "eta_h": eta, "confidence": 0.88, "provider": "builtin"}


@mcp.tool()
async def estimate_cost(distance_km: float, cost_per_km: float = 2.6,
                        temp_ctrl: bool = False, hazmat: bool = False) -> dict:
    """运输成本估算：里程单价 + 起步价 + 温控/危品附加（选定承运商后精算）。"""
    return {"ok": True, "distance_km": distance_km, "cost_per_km": cost_per_km,
            "est": _cost(distance_km, cost_per_km, temp_ctrl, hazmat), "currency": "CNY"}


@mcp.tool()
async def geocode(name: str) -> dict:
    """城市/地名解析为内置路网节点（供需求解析与订单落单用）。"""
    c = _norm_city(name)
    return {"ok": bool(c), "input": name, "city": c,
            "coord": {"lng": _CITY_DB[c][0], "lat": _CITY_DB[c][1]} if c else None}


def _route_tool_executor(name: str, args: Dict[str, Any]) -> str:
    """S4 原生 function calling 的本地工具执行器（同步）。

    语义与 geocode / estimate_eta 一致，但走内部同步辅助函数（_norm_city/_road_km/
    _eta_hours），避免异步事件循环跨线程（_chat_with_tools 在 asyncio.to_thread 中
    执行）。返回 JSON 字符串，供模型消费。
    """
    if name == "geocode":
        c = _norm_city(str(args.get("name", "")))
        if c:
            lng, lat = _CITY_DB[c]
            return json.dumps({"ok": True, "city": c, "coord": {"lng": lng, "lat": lat}},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "input": args.get("name")}, ensure_ascii=False)
    if name == "estimate_eta":
        o = _norm_city(str(args.get("origin", ""))) if args.get("origin") else None
        d = _norm_city(str(args.get("dest", ""))) if args.get("dest") else None
        mode = str(args.get("mode") or "truck")
        if o and d:
            km = _road_km(o, d)
            return json.dumps({"ok": True, "origin": o, "dest": d, "mode": mode,
                               "distance_km": km, "eta_h": _eta_hours(km, mode)},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "error": "需提供有效 origin/dest"}, ensure_ascii=False)
    return json.dumps({"ok": False, "error": f"unknown tool {name}"}, ensure_ascii=False)


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
