"""在途天气监控：天气恶化时自动生成异常单，而不是等司机打电话。

改造前的缺口
------------
内核的 `detect_exception` 有 R5 规则（暴雨/暴雪/台风/大雾/高温 → 天气类异常），
但**平台侧完全没有**：外部的雨雪台风只能靠承运商上报，没上报系统就当没发生。
承运商正忙着开车，通常不会上报。

这里补的是"系统自己发现"：扫描在途订单的路线天气，命中恶劣天气就建异常单，
走与人工上报完全相同的处置链（分级 → SOP → 改道/改期/改派）。

为什么不放进定时任务
--------------------
平台是单进程 + 一键 .bat，没有 worker。这里做成**可手动触发的扫描接口**，
由调度台按钮或 overview 调用。频率低、代价可控，也不会因为一次外部 API 抖动
就把启动流程拖住。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ExceptionReport, OrderPlan, ShipperOrder
from . import weather_risk

# 天气现象 -> 故障谱异常码与等级（与内核 R5 口径一致，不另立一套）
_WX_CODES = (
    ("台风", "E-WX-TYPHOON", "P0", "台风"),
    ("暴雪", "E-WX-SNOW", "P1", "暴雪"),
    ("大雪", "E-WX-SNOW", "P1", "大雪"),
    ("暴雨", "E-WX-RAIN", "P2", "暴雨"),
    ("大雨", "E-WX-RAIN", "P2", "大雨"),
    ("大雾", "E-WX-FOG", "P2", "大雾"),
    ("高温", "E-WX-HOT", "P2", "高温"),
)

DEFAULT_LIMIT = 20


def _code_of(condition: str) -> Optional[tuple]:
    text = str(condition or "")
    for word, code, level, name in _WX_CODES:
        if word in text:
            return code, level, name
    return None


def _cities_of(db: Session, order: ShipperOrder) -> List[str]:
    plan = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
    if plan and isinstance(plan.route_json, dict):
        wp = [str(w) for w in (plan.route_json.get("waypoints") or []) if w]
        if wp:
            return wp
    return [x for x in (order.origin, order.dest) if x]


def scan_inflight(db: Session, limit: int = DEFAULT_LIMIT,
                  dry_run: bool = False) -> Dict[str, Any]:
    """扫描在途订单路线天气，恶劣的建异常单。

    幂等：同一订单已存在**未处置**的天气异常就不再重复建单——否则调度台会被
    同一场雨刷出几十条待处置。
    """
    orders = db.execute(select(ShipperOrder).where(
        ShipperOrder.status == "in_transit").order_by(
        ShipperOrder.id.desc()).limit(limit)).scalars().all()

    created: List[Dict[str, Any]] = []
    checked = 0
    warned = 0

    # 已在待处置的天气异常（按订单去重）
    open_wx = {e.order_id for e in db.execute(select(ExceptionReport).where(
        ExceptionReport.status != "resolved",
        ExceptionReport.exception_code.like("E-WX-%"))).scalars().all()}

    for order in orders:
        cities = _cities_of(db, order)
        if not cities:
            continue
        checked += 1
        risk = weather_risk.assess(cities, order.cargo_type or "普货")
        if not risk.get("ok") or float(risk.get("factor") or 0) <= 0:
            continue
        warned += 1
        mapped = _code_of(risk.get("condition"))
        if not mapped:
            continue
        code, level, name = mapped
        if order.id in open_wx:
            continue
        desc = (f"{'、'.join(cities[:3])} 沿途{risk.get('condition')}"
                f"（{risk.get('note')}）")
        if dry_run:
            created.append({"order_id": order.id, "order_no": order.order_no,
                            "code": code, "level": level, "would_create": True,
                            "description": desc})
            continue
        e = ExceptionReport(order_id=order.id,
                            reporter_user_id=order.shipper_user_id,
                            exception_code=code, exception_name=name,
                            description=desc, level=level, status="open")
        db.add(e)
        db.flush()
        created.append({"order_id": order.id, "order_no": order.order_no,
                        "exception_id": e.id, "code": code, "level": level,
                        "description": desc})
        open_wx.add(order.id)

    if not dry_run:
        db.commit()
    return {"checked": checked, "warned": warned, "created": created,
            "dry_run": dry_run,
            "note": (f"扫描 {checked} 条在途订单，{warned} 条路线天气影响运输，"
                     f"新建 {len(created)} 条异常单"
                     + ("（仅预览，未落库）" if dry_run else ""))}


__all__ = ["scan_inflight", "DEFAULT_LIMIT"]
