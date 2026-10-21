"""真实改道：把"受影响城市"从途经点里剔除，重新问一次真实路径。

改造前的状况
------------
`triage.resolve(action="replan")` 与 `resume` 的落库结果完全相同（都回 `in_transit`），
只往事件里写了一个 `action: replan` 字段。页面和 Agent 话术却说"改道后继续"——
**文案跑在实现前面**。这个模块把它补上。

怎么算"真实"
------------
1. 受影响城市只从异常描述/上报位置里提取，提不到就**不改道**（返回 skipped 并说明），
   绝不猜一个城市来凑里程；
2. 改道前与改道后**同源重算**（都走 `map-mcp.plan_route_via`），这样 `detour_km`
   是两次真实规划的差，而不是"新路线真实里程 − 旧路线几何估算"这种不可比的差值；
3. 高德不可用时两段都退回几何估算并标 `provider=builtin-road-network`，
   页面必须显示"非真实路况"。

能力边界（对外必须这么说）
--------------------------
高德 v3 driving 没有"避开某城市"参数，绕行靠**改途经点**实现，所以这是
**城市级绕行**，不是路段级避让；也不承诺是全局最优绕行。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..mcp_bridge import McpTimeout, run_async
from ..models import OrderEvent, OrderPlan, OrderPublish, ShipperOrder
from . import audit, routing

# 受影响城市的"影响半径"：距该城市 60km 以内的途经点视为会被波及，需要剔除。
# 60km 是城市群尺度，不是收费站尺度——再细就要求路段级数据了，高德 v3 给不了。
IMPACT_RADIUS_KM = 60.0

DEFAULT_COST_PER_KM = Decimal("2.60")
SPEED_KMH = 60.0


def _haversine_city(a: str, b: str) -> Optional[float]:
    """两个城市键之间的距离（km）；任一不在内置库则返回 None。"""
    try:
        from sca.mcp.map_server import _CITY_DB, _haversine

        if a in _CITY_DB and b in _CITY_DB:
            return float(_haversine(a, b))
    except Exception:  # noqa: BLE001 距离算不出来不应让改道整体失败
        pass
    return None


def locate_city(text: str) -> Tuple[Optional[str], str]:
    """从一段文字里定位受影响城市。

    返回 `(city_key, source)`。`source` 说明依据，便于页面上写清"这是从哪认出来的"。
    顺序：城市名直接命中 -> 省名按货运惯例映射到省会 -> 无法定位。
    省名不硬猜具体城市，只按"省会代表该省"这一条既有规则处理，并在结果里标注来源。
    """
    if not text:
        return None, ""
    city = routing.city_in_text(text)
    if city:
        return city, "描述中的城市名"
    try:
        from sca.mcp.map_server import _PROVINCE_CAPITAL

        for prov, capital in _PROVINCE_CAPITAL.items():
            # 省名要 2 字以上才认，避免单字误伤；命中后按省会计
            if len(prov) >= 2 and prov in text:
                return capital, f"描述中的省名「{prov}」→ 省会 {capital}"
    except Exception:  # noqa: BLE001
        pass
    return None, ""


async def _plan_via_async(waypoints: List[str], order: Optional[ShipperOrder],
                          cost_per_km: float, avoid_city: str = "") -> Dict[str, Any]:
    from sca.mcp.client import call as mcp_call

    cargo = (order.cargo_type if order else "") or "普货"
    r = await mcp_call("map-mcp", "plan_route_via", waypoints=list(waypoints),
                       cargo_type=cargo,
                       temp_ctrl=bool(order.temp_ctrl) if order else False,
                       hazmat=bool(order.hazmat) if order else False,
                       cost_per_km=cost_per_km,
                       avoid_city=avoid_city or "",
                       avoid_radius_km=IMPACT_RADIUS_KM)
    if r.ok and isinstance(r.data, dict):
        return r.data
    return {"ok": False, "error": r.error or "map-mcp 不可用"}


def _plan_via(waypoints: List[str], order: Optional[ShipperOrder],
              cost_per_km: float, avoid_city: str = "") -> Dict[str, Any]:
    try:
        return run_async(_plan_via_async(waypoints, order, cost_per_km, avoid_city),
                         timeout=25)
    except McpTimeout:
        return {"ok": False, "error": "路径重算超时"}
    except Exception as exc:  # noqa: BLE001 外部服务异常统一降级
        return {"ok": False, "error": f"路径重算失败：{type(exc).__name__}: {exc}"}


def _cost_per_km(db: Session, order: ShipperOrder) -> float:
    """成本单价：已接单用承运商自己的单价，否则用平台默认（与出方案同口径）。"""
    try:
        from .carrier_ops import profile_of

        p = profile_of(db, order.carrier_org_id) if order.carrier_org_id else None
        if p and p.cost_per_km:
            return float(p.cost_per_km)
    except Exception:  # noqa: BLE001
        pass
    return float(DEFAULT_COST_PER_KM)


def _old_waypoints(db: Session, order: ShipperOrder) -> List[str]:
    """取当前方案的途经点；没有方案时按起讫重算，不留空。"""
    plan = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
    if plan and isinstance(plan.route_json, dict):
        wp = [str(w) for w in (plan.route_json.get("waypoints") or []) if w]
        if len(wp) >= 2:
            return wp
    o, d = routing.resolve_city(order.origin), routing.resolve_city(order.dest)
    if not o or not d:
        return []
    try:
        from sca.mcp.map_server import _pick_hubs

        return [o] + list(_pick_hubs(o, d)) + [d]
    except Exception:  # noqa: BLE001
        return [o, d]


def preview_reroute(db: Session, order: ShipperOrder, *,
                    position_text: str = "", description: str = "",
                    avoid_city: str = "") -> Dict[str, Any]:
    """算一份改道方案，**不落库**。页面先展示对比，调度确认后才 apply。

    返回值里的 `ok=False` 一律带 `reason`，前端要原样展示——
    "没改道"和"改道了但没区别"是两件事，不能糊在一起。
    """
    o = routing.resolve_city(order.origin)
    d = routing.resolve_city(order.dest)
    if not o or not d:
        return {"ok": False, "reason": f"起讫地无法解析（{order.origin} / {order.dest}），不生成改道方案"}

    text = " ".join(x for x in (avoid_city, position_text, description) if x).strip()
    city, source = locate_city(text)
    if not city:
        return {"ok": False, "skipped": True,
                "reason": "异常描述里没有可定位的城市/路段，不生成改道方案"
                          "（避免编造绕行里程）；可在「受影响路段」里补录具体地名"}
    if city in (o, d):
        return {"ok": False, "skipped": True,
                "reason": f"受影响城市「{city}」就是起运地或目的地，无法通过改道规避"}

    old_wp = _old_waypoints(db, order)
    if len(old_wp) < 2:
        return {"ok": False, "skipped": True, "reason": "缺少可用途经点，无法计算改道"}

    removed: List[str] = []
    kept: List[str] = []
    for w in old_wp:
        dist = _haversine_city(w, city)
        if w in (o, d) or dist is None or dist > IMPACT_RADIUS_KM:
            kept.append(w)
        else:
            removed.append(w)

    if not removed:
        return {"ok": False, "skipped": True, "avoid_city": city, "source": source,
                "reason": f"受影响城市「{city}」不在当前规划走廊的途经点上"
                          f"（沿线最近的点都超过 {IMPACT_RADIUS_KM:.0f}km），"
                          f"改道不会改变路线"}

    cpm = _cost_per_km(db, order)
    base = _plan_via(old_wp, order, cpm, avoid_city=city)
    if not base.get("ok"):
        return {"ok": False, "reason": base.get("error") or "原路线重算失败"}
    # 真实路径采样点没落在该城市半径内 -> 本来就不经过，改道没有意义
    if base.get("avoid_hit") is False:
        return {"ok": False, "skipped": True, "avoid_city": city, "source": source,
                "reason": f"原路线的真实路径未经过「{city}」"
                          f"（{IMPACT_RADIUS_KM:.0f}km 内无采样点），改道不会改变路线"}

    # 候选走廊：① 只剔除受影响点；② 连它两侧相邻点一起剔除（郑州→栾川必经洛阳一带，
    # 只删洛阳是删不掉的，必须让它绕开整个区段）
    tries: List[List[str]] = [kept]
    idxs = [i for i, w in enumerate(old_wp) if w in removed]
    drop = set()
    for i in idxs:
        for j in (i - 1, i, i + 1):
            if 1 <= j <= len(old_wp) - 2:
                drop.add(j)
    kept2 = [w for i, w in enumerate(old_wp) if i not in drop]
    if kept2 and kept2 != kept and len(kept2) >= 2:
        tries.append(kept2)

    new: Optional[Dict[str, Any]] = None
    used: List[str] = []
    for cand in tries:
        r = _plan_via(cand, order, cpm, avoid_city=city)
        if not r.get("ok"):
            continue
        new, used = r, cand
        if r.get("avoid_hit") is False:
            break          # 已验证真的绕开
        if r.get("avoid_hit") is None:
            break          # 高德不可用，无法验证，收下并如实标注
    if not new:
        return {"ok": False, "reason": "改道候选均无法完成路径重算"}
    if new.get("avoid_hit") is True:
        return {"ok": False, "skipped": True, "avoid_city": city, "source": source,
                "reason": f"把「{city}」从途经点剔除后，高德规划出的路线仍穿过该城市"
                          f"（{IMPACT_RADIUS_KM:.0f}km 内），无法可靠绕开——"
                          f"建议延长时效或改派承运商，而不是宣称已改道"}

    dist_old = float(base.get("distance_km") or 0)
    dist_new = float(new.get("distance_km") or 0)
    if dist_old <= 0 or dist_new <= 0:
        return {"ok": False, "reason": "重算里程为 0，放弃本次改道（不写假数据）"}

    # 口径统一：里程与 ETA 都按平台方案口径（里程/60），成本按单价，
    # 这样改道前后、以及与原方案之间可以直接比。map-mcp 的真实 ETA 另存备查。
    eta_old = round(dist_old / SPEED_KMH, 1)
    eta_new = round(dist_new / SPEED_KMH, 1)
    cost_old = round(dist_old * cpm, 2)
    cost_new = round(dist_new * cpm, 2)

    plan_row = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
    plan_dist = float(plan_row.distance_km) if plan_row and plan_row.distance_km else None

    # 改动幅度过大要提示人工确认：可能是途经点本身选得偏，也可能是避让把走廊整体改掉了
    ratio = abs(dist_new - dist_old) / dist_old if dist_old else 0.0
    warn = ""
    if ratio > 0.25:
        warn = (f"改道幅度较大（{'增' if dist_new > dist_old else '减'} "
                f"{abs(dist_new - dist_old):.0f}km，{ratio * 100:.0f}%），请确认新途经点是否合理")

    # 原方案里程是几何估算（直线 × 公路系数），高德真实里程往往更长。
    # 两者差距大时必须显式说明，否则调度会以为"改道把里程从 2164 改成了 3165"。
    baseline_gap: Optional[Dict[str, Any]] = None
    if plan_dist and plan_dist > 0:
        gap = round(dist_old - plan_dist, 1)
        if abs(gap) / plan_dist > 0.15:
            baseline_gap = {"plan_km": round(plan_dist, 1),
                            "real_km": round(dist_old, 1),
                            "gap_km": gap,
                            "gap_pct": round(abs(gap) / plan_dist * 100, 1)}
            warn = (warn + "；" if warn else "") + (
                f"原方案里程 {plan_dist:.0f}km 为几何估算，真实路况 {dist_old:.0f}km"
                f"（差 {gap:+.0f}km），本次改道按真实路况重算")

    return {"ok": True, "avoid_city": city, "source": source,
            "radius_km": IMPACT_RADIUS_KM,
            "removed": removed, "kept": used,
            "avoid_verified": new.get("avoid_hit") is False,
            "provider": new.get("provider"),
            "real_note": ("高德真实路径（改道前后同源重算，已校验避让）"
                          if new.get("provider") == "amap" and new.get("avoid_hit") is False
                          else "高德真实路径（改道前后同源重算，但未能验证避让）"
                          if new.get("provider") == "amap"
                          else "高德不可达：按几何估算（非真实路况，无法验证是否绕开）"),
            "warn": warn,
            "baseline_gap": baseline_gap,
            "old": {"waypoints": old_wp, "distance_km": round(dist_old, 1),
                    "eta_h": eta_old, "cost_est": cost_old,
                    "map_eta_h": base.get("eta_h"), "plan_distance_km": plan_dist},
            "new": {"waypoints": used, "distance_km": round(dist_new, 1),
                    "eta_h": eta_new, "cost_est": cost_new,
                    "map_eta_h": new.get("eta_h")},
            "detour_km": round(dist_new - dist_old, 1),
            "eta_delta_h": round(eta_new - eta_old, 1),
            "cost_delta": round(cost_new - cost_old, 2)}


def apply_reroute(db: Session, order: ShipperOrder, preview: Dict[str, Any],
                  *, reason: str, user: Dict[str, Any], ip: str = "") -> Dict[str, Any]:
    """把预览结果落成新方案版本。只应由 route/service 在人工确认后调用。"""
    if not preview.get("ok"):
        raise ValueError(preview.get("reason") or "改道方案无效")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ValueError("请填写改道原因（至少 2 个字）")

    old = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
    ver = db.execute(select(func.max(OrderPlan.version)).where(
        OrderPlan.order_id == order.id)).scalar() or 0

    new = preview["new"]
    route_json = {
        "ok": True, "origin": order.origin, "dest": order.dest,
        "waypoints": new["waypoints"], "distance_km": new["distance_km"],
        "eta_h": new["eta_h"], "provider": preview.get("provider"),
        "avoid_city": preview.get("avoid_city"),
        "removed_waypoints": preview.get("removed"),
        "avoid_verified": preview.get("avoid_verified"),
        "baseline_gap": preview.get("baseline_gap"),
        "real_note": preview.get("real_note"),
        "map_eta_h": new.get("map_eta_h"),
        "previous": {"waypoints": preview["old"]["waypoints"],
                     "distance_km": preview["old"]["distance_km"]},
        "rerouted_at": datetime.now().isoformat(timespec="minutes"),
    }
    plan = OrderPlan(order_id=order.id, version=int(ver) + 1, route_json=route_json,
                     distance_km=Decimal(str(new["distance_km"])),
                     eta_h=Decimal(str(new["eta_h"])),
                     cost_est=Decimal(str(new["cost_est"])),
                     carrier_org_id=order.carrier_org_id,
                     created_by_user_id=user.get("user_id"))
    db.add(plan)
    db.flush()
    order.active_plan_id = plan.id
    order.updated_at = datetime.now()

    # 挂牌中的单要跟着指向新方案；价格不动——挂牌价由「预算 − 费率」决定，
    # 绕行造成的只是成本变化，不该悄悄改价。
    for pub in db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id,
            OrderPublish.status == "open")).scalars().all():
        pub.plan_id = plan.id

    seq = (db.execute(select(func.max(OrderEvent.seq)).where(
        OrderEvent.order_id == order.id)).scalar() or 0) + 1
    payload = {"reason": reason, "avoid_city": preview.get("avoid_city"),
               "source": preview.get("source"),
               "removed": preview.get("removed"),
               "provider": preview.get("provider"),
               "old": {"distance_km": preview["old"]["distance_km"],
                       "eta_h": preview["old"]["eta_h"],
                       "cost_est": preview["old"]["cost_est"]},
               "new": {"distance_km": new["distance_km"], "eta_h": new["eta_h"],
                       "cost_est": new["cost_est"]},
               "detour_km": preview.get("detour_km"),
               "eta_delta_h": preview.get("eta_delta_h"),
               "baseline_gap": preview.get("baseline_gap"),
               "plan_version": plan.version}
    db.add(OrderEvent(order_id=order.id, seq=seq, event_type="order_rerouted",
                      actor_role=user.get("role") or "dispatcher",
                      actor_user_id=user.get("user_id"), payload=payload))
    audit.log(db, user, "order_rerouted", target_type="order", target_id=order.id,
              reason=reason, detail={"order_no": order.order_no,
                                     "detour_km": preview.get("detour_km"),
                                     "plan_version": plan.version}, ip=ip)
    db.commit()
    return {"plan_id": plan.id, "version": plan.version,
            "distance_km": new["distance_km"], "eta_h": new["eta_h"],
            "cost_est": new["cost_est"], "detour_km": preview.get("detour_km"),
            "eta_delta_h": preview.get("eta_delta_h"),
            "avoid_city": preview.get("avoid_city"),
            "provider": preview.get("provider"),
            "previous_plan_id": old.id if old else None,
            "note": f"已改道绕开「{preview.get('avoid_city')}」"
                    f"（{preview.get('real_note')}）"}


__all__ = ["IMPACT_RADIUS_KM", "locate_city", "preview_reroute", "apply_reroute"]
