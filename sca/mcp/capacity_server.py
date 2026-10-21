"""MCP 工具层（FR-11 / FR-02）：运力智能匹配 Server。

多因子真实匹配（非随机取 Top-N）：
  1. 硬约束过滤：载重 ≥ 货重、温控/危品资质、车型适配、承运商可覆盖起运地；
  2. 加权打分：服务质量 40% + 成本 25% + 接驳距离 20% + 时效履约 15%；
  3. 输出 rank_score、各项因子分与"匹配理由"，供前端与审计解释"为什么选它"。
接驳距离复用 map-mcp 的真实路网里程（跨 Server 协作）。
配置了 SCA_CAPACITY_API 时改从真实运力平台拉取候选池（失败降级内置池）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.capacity")
mcp = FastMCP("capacity-mcp") if HAVE_MCP else _StubMCP()

# ---------------------------------------------------------------------------
# 候选承运商池：资质 + 车型 + 成本 + 所在地（用于接驳距离）+ 履约表现
# ---------------------------------------------------------------------------
_CARRIERS: List[Dict[str, Any]] = [
    {"id": "C001", "name": "华运冷链物流", "vehicle": "9.6m 冷藏车", "capacity_t": 12,
     "temp_ctrl": True, "hazmat": False, "score": 4.7, "cost_per_km": 3.2,
     "city": "上海", "on_time_rate": 0.96, "fleet": 42},
    {"id": "C002", "name": "顺达快运", "vehicle": "13m 高栏车", "capacity_t": 18,
     "temp_ctrl": False, "hazmat": False, "score": 4.5, "cost_per_km": 2.6,
     "city": "广州", "on_time_rate": 0.93, "fleet": 120},
    {"id": "C003", "name": "中通干线运输", "vehicle": "13m 厢式货车", "capacity_t": 16,
     "temp_ctrl": False, "hazmat": False, "score": 4.3, "cost_per_km": 2.1,
     "city": "杭州", "on_time_rate": 0.90, "fleet": 200},
    {"id": "C004", "name": "安能危品专线", "vehicle": "危险品专用车", "capacity_t": 10,
     "temp_ctrl": False, "hazmat": True, "score": 4.6, "cost_per_km": 3.8,
     "city": "天津", "on_time_rate": 0.97, "fleet": 28},
    {"id": "C005", "name": "京翼冷藏干线", "vehicle": "15m 双温区冷藏车", "capacity_t": 22,
     "temp_ctrl": True, "hazmat": False, "score": 4.4, "cost_per_km": 3.0,
     "city": "北京", "on_time_rate": 0.94, "fleet": 60},
    {"id": "C006", "name": "川渝专线联盟", "vehicle": "17.5m 平板车", "capacity_t": 30,
     "temp_ctrl": False, "hazmat": False, "score": 4.0, "cost_per_km": 1.9,
     "city": "成都", "on_time_rate": 0.87, "fleet": 85},
    {"id": "C007", "name": "中远危化运输", "vehicle": "危化品罐车", "capacity_t": 24,
     "temp_ctrl": True, "hazmat": True, "score": 4.8, "cost_per_km": 4.2,
     "city": "青岛", "on_time_rate": 0.98, "fleet": 35},
    {"id": "C008", "name": "华中零担快线", "vehicle": "9.6m 厢式货车", "capacity_t": 8,
     "temp_ctrl": False, "hazmat": False, "score": 4.1, "cost_per_km": 2.3,
     "city": "武汉", "on_time_rate": 0.91, "fleet": 150},
    {"id": "C009", "name": "东北冷链联运", "vehicle": "12m 冷藏半挂", "capacity_t": 26,
     "temp_ctrl": True, "hazmat": False, "score": 4.2, "cost_per_km": 2.9,
     "city": "沈阳", "on_time_rate": 0.92, "fleet": 48},
    {"id": "C010", "name": "西北大宗专线", "vehicle": "17.5m 高低板", "capacity_t": 32,
     "temp_ctrl": False, "hazmat": False, "score": 3.9, "cost_per_km": 1.7,
     "city": "西安", "on_time_rate": 0.85, "fleet": 70},
    # --- 华北 / 华东普通货运干线（2026-10-15 补充，样本数据）---
    # 背景：原样本池 10 家全部为冷链/危品/西部大宗车，华北华东无普货运力，
    # 导致"河北→南京 20t 普货"这类常规单在池内找不到合理承运商。
    {"id": "C011", "name": "冀运普货干线", "vehicle": "13m 高栏车", "capacity_t": 25,
     "temp_ctrl": False, "hazmat": False, "score": 4.4, "cost_per_km": 2.4,
     "city": "石家庄", "on_time_rate": 0.95, "fleet": 96},
    {"id": "C012", "name": "齐鲁干线物流", "vehicle": "17.5m 厢式货车", "capacity_t": 30,
     "temp_ctrl": False, "hazmat": False, "score": 4.3, "cost_per_km": 2.2,
     "city": "济南", "on_time_rate": 0.93, "fleet": 140},
    {"id": "C013", "name": "长三角干线运输", "vehicle": "13m 厢式货车", "capacity_t": 20,
     "temp_ctrl": False, "hazmat": False, "score": 4.5, "cost_per_km": 2.5,
     "city": "南京", "on_time_rate": 0.96, "fleet": 110},
    {"id": "C014", "name": "京津普货专线", "vehicle": "9.6m 厢式货车", "capacity_t": 12,
     "temp_ctrl": False, "hazmat": False, "score": 4.2, "cost_per_km": 2.3,
     "city": "天津", "on_time_rate": 0.94, "fleet": 88},
]

# 运力占用状态（2026-10-15 用户要求）：
#   order_id = 该承运商**当前正在承运**的运单号，空串表示未接单；
#   idle     = 是否空闲可派（有在途订单即非空闲）。
# 二者必须一致：order_id 非空 -> idle=False。这里只对内置池补默认值，
# 真实数据以 data/carriers.json 为准（carriers_store 负责读写与规范化）。
for _c in _CARRIERS:
    _c.setdefault("order_id", "")
    _c.setdefault("idle", not _c["order_id"])

# 打分权重（服务 / 成本 / 接驳 / 时效）
_WEIGHTS = {"score": 0.40, "cost": 0.25, "pickup": 0.20, "on_time": 0.15}
# "配置超出需求"惩罚：未要求温控/危品却派冷藏车/危品车，属运力错配——
# 车况资质过剩、单价普遍更高，且挤占了真正合适的普货运力。按项扣分（不硬过滤，
# 保证极端场景（池内只有特种车）仍能给出兜底结果）。
_OVER_SPEC_PENALTY = {"temp": 0.08, "hazmat": 0.10}


def _split_route(route: str) -> tuple:
    """解析 '上海→北京' / '上海-北京' / '上海 到 北京' -> (origin, dest)。"""
    if not route:
        return None, None
    s = str(route)
    for sep in ("→", "->", "至", "到", "-"):
        if sep in s:
            a, b = s.split(sep, 1)
            return a.strip(), b.strip()
    return None, None


def _pickup_km(carrier_city: str, origin: Optional[str]) -> float:
    """接驳距离：承运商所在地 -> 起运地（复用 map-mcp 真实路网）。"""
    if not origin:
        return 0.0
    try:
        from sca.mcp.map_server import _norm_city, _road_km
        c, o = _norm_city(carrier_city), _norm_city(origin)
        if c and o:
            return _road_km(c, o)
    except Exception as exc:  # 路网不可用时不阻塞匹配
        logger.debug("[capacity] 接驳距离计算失败：%s", exc)
    return 0.0


async def _pool() -> List[Dict[str, Any]]:
    """候选池：配置了真实运力平台则拉取，否则用持久化承运商库（data/carriers.json）。"""
    api = os.environ.get("SCA_CAPACITY_API", "").strip()
    if not api:
        from sca.mcp.carriers_store import load_carriers
        return load_carriers()
    import json

    from sca.util.aio import fetch_bytes
    try:
        data = json.loads((await fetch_bytes(api, timeout=6)).decode("utf-8"))
        if isinstance(data, list) and data:
            return data
    except Exception as exc:
        logger.warning("[capacity] 真实运力平台不可用，降级承运商库：%s", exc)
    from sca.mcp.carriers_store import load_carriers
    return load_carriers()


def _score_carrier(c: Dict[str, Any], pk: float, need_temp: bool, need_haz: bool,
                   distance_km: float | None) -> Dict[str, Any]:
    """多因子打分：服务 40% + 成本 25% + 接驳 20% + 时效 15%，再扣"配置超出需求"惩罚，
    并给出可解释理由。"""
    reasons: List[str] = []
    f_score = min(float(c.get("score", 0)) / 5.0, 1.0)
    f_cost = max(0.0, min(1.0, (4.2 - float(c.get("cost_per_km", 3.0))) / 2.5))
    f_pickup = max(0.0, min(1.0, 1 - pk / 800.0))
    f_ontime = float(c.get("on_time_rate", 0.9))
    base = (_WEIGHTS["score"] * f_score + _WEIGHTS["cost"] * f_cost +
            _WEIGHTS["pickup"] * f_pickup + _WEIGHTS["on_time"] * f_ontime)
    # 运力错配扣分：派了用不上的特种车（未要求温控/危品）
    penalty = 0.0
    if c.get("temp_ctrl") and not need_temp:
        penalty += _OVER_SPEC_PENALTY["temp"]
        reasons.append(f"温控车型（本单未要求，单价 ¥{c.get('cost_per_km')}/km 偏高）")
    if c.get("hazmat") and not need_haz:
        penalty += _OVER_SPEC_PENALTY["hazmat"]
        reasons.append(f"危品车型（本单未要求，单价 ¥{c.get('cost_per_km')}/km 偏高）")
    rank = max(0.0, base - penalty)
    if f_score >= 0.9:
        reasons.append(f"服务质量优秀 ★{c.get('score')}")
    if f_cost >= 0.7:
        reasons.append(f"单价低 ¥{c.get('cost_per_km')}/km")
    if pk <= 200:
        reasons.append(f"接驳近 {pk}km")
    if f_ontime >= 0.95:
        reasons.append(f"准时率 {int(f_ontime * 100)}%")
    if need_temp:
        reasons.append("具备温控资质")
    if need_haz:
        reasons.append("具备危品资质")
    return {**c, "pickup_km": round(pk, 1), "rank_score": round(rank, 4),
            "factors": {"service": round(f_score, 3), "cost": round(f_cost, 3),
                        "pickup": round(f_pickup, 3), "on_time": round(f_ontime, 3),
                        "over_spec_penalty": round(penalty, 3)},
            "match_reasons": reasons or ["综合评分入选"],
            "est_cost": round((distance_km or 0) * float(c.get("cost_per_km", 2.6)) + 300.0, 1)}


@mcp.tool()
async def match_capacity(cargo_type: str = "普货", route: str = "", top_n: int = 3,
                         weight_t: float = 5.0, temp_ctrl: bool = False,
                         hazmat: bool = False, distance_km: float | None = None,
                         order_id: str = "") -> list:
    """运力智能匹配（FR-02）：资质硬过滤 + 多因子加权排序，返回 Top-N 与匹配理由。

    order_id（可选）：调度员在异常描述里录入的运单号。命中池中"正承运该单"的承运商时
    置顶并标注——异常处置要沿用**原车原司机**，不能因为一次重算就换承运商。
    """
    origin, dest = _split_route(route)
    pool = await _pool()
    need_temp = temp_ctrl or cargo_type == "冷链"
    need_haz = hazmat or cargo_type == "危险品"

    eligible, rejected, far_eligible = [], [], []
    for c in pool:
        reasons = []
        if float(c.get("capacity_t", 0)) < float(weight_t):
            rejected.append({"id": c["id"], "name": c["name"], "reason": f"载重 {c.get('capacity_t')}t < {weight_t}t（不满足）"})
            continue
        if need_temp and not c.get("temp_ctrl"):
            rejected.append({"id": c["id"], "name": c["name"], "reason": "无温控资质"})
            continue
        if need_haz and not c.get("hazmat"):
            rejected.append({"id": c["id"], "name": c["name"], "reason": "无危险品运输资质"})
            continue
        pk = _pickup_km(c.get("city", ""), origin)
        if pk > 800:   # 接驳过远，不经济；但若最终无可用运力则放宽（见下方降级）
            rejected.append({"id": c["id"], "name": c["name"], "reason": f"接驳距离 {pk}km 过远"})
            far_eligible.append(c)
            continue
        eligible.append(_score_carrier(c, pk, need_temp, need_haz, distance_km))

    # 订单号绑定（2026-10-15 用户要求）：先看池里哪家承运商正承运该单
    # （carriers.order_id == 订单号）。命中且通过硬约束 -> 置顶，理由写明；命中但被
    # 硬约束淘汰 -> 不强行置顶（数据不一致时如实暴露，不用不合格的车掩盖问题）。
    bound_id, bound_note = "", ""
    eligible_sorted = sorted(eligible, key=lambda x: -x["rank_score"])
    if order_id:
        want = str(order_id).strip()
        for c in pool:
            if str(c.get("order_id") or "").strip() == want:
                bound_id = str(c.get("id"))
                break
    if bound_id:
        picked = next((r for r in eligible_sorted if str(r.get("id")) == bound_id), None)
        if picked is not None:
            picked = {**picked, "bound_order_id": str(order_id),
                      "match_reasons": [f"承接运单 {order_id}（在途承运商，优先续用）"]
                                       + list(picked.get("match_reasons") or [])}
            eligible_sorted = [picked] + [r for r in eligible_sorted
                                          if str(r.get("id")) != bound_id]
        else:
            bound_note = (f"运单 {order_id} 的原承运商 {bound_id} 未通过本次硬约束"
                          f"（载重/资质/接驳），已按重新匹配结果给出候选")

    ranked = eligible_sorted[:top_n]
    relaxed = False
    if not ranked and far_eligible:
        # 降级：放行"接驳过远"但资质合格的运力，并明确标注（不静默放宽）
        relaxed = True
        ranked = sorted([_score_carrier(c, _pickup_km(c.get("city", ""), origin),
                                        need_temp, need_haz, distance_km)
                         for c in far_eligible], key=lambda x: -x["rank_score"])[:top_n]
        for r in ranked:
            r["match_reasons"].append(f"接驳较远 {r['pickup_km']}km（本地无更近合规运力，已放宽）")
    # 记录一次过滤说明（供审计/看板）
    logger.info("[capacity] 需求 %s %st 温控=%s 危品=%s -> 合格 %d / 淘汰 %d",
                cargo_type, weight_t, need_temp, need_haz, len(eligible), len(rejected))
    if not ranked:
        return []
    ranked[0]["_meta"] = {"pool_size": len(pool), "eligible": len(eligible),
                          "rejected": rejected[:5], "weights": _WEIGHTS,
                          "route": route, "origin": origin, "dest": dest,
                          "relaxed": relaxed,
                          # 订单号绑定结果：本次沿用哪家承运商（未命中则空）
                          "query_order_id": str(order_id or "").strip(),
                          "bound_carrier_id": bound_id, "bound_note": bound_note}
    return ranked


@mcp.tool()
async def rank_carriers(carriers: list, weight_cost: float = 0.5, weight_score: float = 0.5) -> list:
    """对候选运力多维打分排序（成本 + 评分，可自定义权重）。"""
    out = []
    for c in carriers:
        s = weight_score * float(c.get("score", 0)) + weight_cost * (5.0 - float(c.get("cost_per_km", 3.0)))
        out.append({**c, "rank_score": round(s, 3)})
    return sorted(out, key=lambda x: -x["rank_score"])


@mcp.tool()
async def list_carriers() -> list:
    """承运商资源池清单（供看板/核对）。"""
    pool = await _pool()
    return [{"id": c.get("id"), "name": c.get("name"), "vehicle": c.get("vehicle"),
             "capacity_t": c.get("capacity_t"), "temp_ctrl": c.get("temp_ctrl"),
             "hazmat": c.get("hazmat"), "city": c.get("city"),
             "score": c.get("score"), "cost_per_km": c.get("cost_per_km"),
             # 运力占用：当前承运订单号 + 是否空闲（有在途单即非空闲）
             "order_id": c.get("order_id") or "",
             "idle": bool(c.get("idle", not (c.get("order_id") or ""))),
             "driver_name": c.get("driver_name") or "",
             "driver_phone": c.get("driver_phone") or ""} for c in pool]


@mcp.tool()
async def upsert_carrier(carrier: dict) -> dict:
    """新增或覆盖一个承运商（按 id）。返回落盘后的完整记录；缺 id 自动生成。"""
    from sca.mcp.carriers_store import upsert_carrier as _upsert
    rec = _upsert(dict(carrier or {}))
    return {"ok": True, "carrier": rec}


@mcp.tool()
async def delete_carrier(carrier_id: str) -> dict:
    """删除指定 id 的承运商；返回是否删除成功。"""
    from sca.mcp.carriers_store import delete_carrier as _del
    ok = _del(str(carrier_id))
    return {"ok": ok, "carrier_id": carrier_id,
            "message": "已删除" if ok else "未找到该承运商"}


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
