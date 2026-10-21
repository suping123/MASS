"""抓取全国省/市/区县（县级市+县）中心点经纬度 -> sca/mcp/china_cities.json

数据来源：阿里 DataV 行政区划边界数据（geo.datav.aliyun.com），
其底图基于"国家地理信息公共服务平台（天地图）"标准地图，属合规来源。

两级抓取：
  1. 100000_full.json                -> 34 个省级中心点；
  2. {省级adcode}_full.json          -> 各地级市中心点；
  3. {地级市adcode}_full.json        -> 区县；仅保留"县级市/县"（name 以 市/县 结尾），
                                      过滤掉市区（如"天河区""市辖区"）以控制规模。

用途与合规声明：
  * 仅用于**本地里程估算**（城市中心点 haversine × 公路系数），
    不渲染地图、不调用任何在线地图服务、不含导航路径；
  * 只取行政区划**中心点经纬度**（地理事实），不涉及国界/界线绘制，
    不含军事禁区、保密单位等敏感点位；
  * 不采集、不存储任何个人信息位置。

输出：{"城市名": [lng, lat], ...}，供 sca/mcp/map_server.py 作为内置路网城市库加载。
运行：.venv\\Scripts\\python.exe scripts/fetch_cities.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

try:  # Windows 控制台中文/长行输出兜底
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa
    pass

BASE = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
TOP = "https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json"
OUT = Path(__file__).resolve().parent.parent / "sca" / "mcp" / "china_cities.json"

# 行政区划后缀：仅用于生成"人话"地名键（如 西昌市 -> 西昌、广西壮族自治区 -> 广西）
_SUFFIX = ("特别行政区", "壮族自治区", "回族自治区", "维吾尔自治区", "自治区",
           "自治州", "省", "市", "地区", "盟", "县")


def _get(url: str, retries: int = 4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:   # 404 = 该级无下级数据（如港澳台），跳过
            if e.code == 404:
                return None
            last = e
        except Exception as exc:  # noqa
            last = exc
        time.sleep(1.5 * (i + 1))
    return None


def _clean(name: str) -> str:
    s = str(name or "").strip()
    for suf in _SUFFIX:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def _add(cities: dict, name, center) -> bool:
    if not (name and center and len(center) == 2):
        return False
    key = _clean(name)
    if not key or key in cities:
        return False
    cities[key] = [round(float(center[0]), 4), round(float(center[1]), 4)]
    return True


def main() -> None:
    top = _get(TOP)
    cities: dict = {}
    province_adcodes: list = []
    for f in top.get("features", []):
        p = f.get("properties", {})
        if p.get("adcode"):
            province_adcodes.append(p["adcode"])
        _add(cities, p.get("name"), p.get("center"))    # 省级中心（含直辖/港澳台）

    # 第二级：地级市
    city_adcodes: list = []
    for adcode in province_adcodes:
        d = _get(BASE.format(adcode=adcode))
        if not d:
            continue
        for f in d.get("features", []):
            p = f.get("properties", {})
            if p.get("level") == "city":
                if _add(cities, p.get("name"), p.get("center")):
                    if p.get("adcode"):
                        city_adcodes.append(p["adcode"])
    print("provinces:", len(province_adcodes), "cities:", len(city_adcodes),
          "total:", len(cities))

    # 第三级：区县（仅县级市/县）
    n = 0
    for adcode in city_adcodes:
        d = _get(BASE.format(adcode=adcode))
        if not d:
            continue
        for f in d.get("features", []):
            p = f.get("properties", {})
            nm = p.get("name", "")
            if p.get("level") == "district" and (nm.endswith("市") or nm.endswith("县")):
                if _add(cities, nm, p.get("center")):
                    n += 1
        time.sleep(0.05)
    print("districts(市/县) added:", n, "total:", len(cities))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cities, ensure_ascii=False, separators=(",", ":"),
                              sort_keys=True), encoding="utf-8")
    print("WROTE", OUT, "count:", len(cities))


if __name__ == "__main__":
    main()
