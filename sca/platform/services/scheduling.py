"""调度动作（出方案 / 定价挂牌）：页面按钮与调度 Agent 共用同一套实现。

抽出来的理由：如果 Agent 自己拼一遍转账逻辑，就会出现"页面点出来的价"和
"Agent 说出来的价"不一致——这是最难查的一类 bug。所有写操作只在这里发生，
路由和 Agent 都只是调用方。

刻意留在服务层之外的东西：谁能做（RBAC，在路由层）、先确认再执行（Agent 侧
的 `_pending`）、审计（写操作内部统一写）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import OrderPlan, OrderPublish, ShipperOrder, SysUser
from . import pricing, runtime
from .orders import add_event, transition
from .routing import estimate_route

DEFAULT_COST_PER_KM = Decimal("2.60")

# 能出方案 / 能挂牌的状态。与页面按钮的显示口径保持一致。
PLANNABLE = ("submitted", "planned")
PUBLISHABLE = ("planned", "published")


class SchedulingError(ValueError):
    """业务性拒绝（状态不对、缺预算等）。路由层转 409，Agent 直接读 message。"""


def preview_plan(order: ShipperOrder, db: Optional[Session] = None) -> Dict[str, Any]:
    """只估算不落库：给出路线、里程、时长、成本与挂牌价预估。

    Agent 在执行前先把它讲给调度员听，调度员确认后才真正写库。

    2026-10-18 起接天气：路线途经城市的恶劣天气会把 ETA 上浮（只上浮不下调）。
    价格**不受天气影响**——挂牌价仍是「预算 − 货主侧费率」的确定性公式，
    天气只改变"预计多久到"，不改变"收多少钱"。
    """
    from . import weather_risk

    r = estimate_route(order.origin, order.dest)
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("reason")}
    dist = Decimal(str(r["distance_km"]))
    base_eta = float(r["eta_h"])
    wp = r.get("waypoints") or [x for x in (order.origin, order.dest) if x]
    risk = weather_risk.assess(wp, order.cargo_type or "普货")
    eta_adj, factor = weather_risk.apply_to_eta(base_eta, risk)
    eta = Decimal(str(eta_adj))
    cost = (dist * DEFAULT_COST_PER_KM).quantize(pricing.CENT)

    gross = Decimal(str(order.budget_max or 0))
    fr, cr = (runtime.active_fee(db) if db is not None
              else (Decimal(str(config.SHIPPER_FEE_RATE)),
                    Decimal(str(config.CARRIER_FEE_RATE))))
    quote = pricing.settle_amounts(gross, fr, cr) if gross > 0 else None
    return {"ok": True, "distance_km": float(dist), "eta_h": float(eta),
            "eta_base_h": base_eta, "weather_factor": factor, "weather": risk,
            "cost_est": float(cost), "cost_per_km": float(DEFAULT_COST_PER_KM),
            "provider": r.get("provider"), "waypoints": r.get("waypoints") or [],
            "gross": float(gross) if gross > 0 else None,
            "publish_price": float(quote["publish"]) if quote else None,
            "shipper_fee": float(quote["shipper_fee"]) if quote else None,
            "fee_rates": {"shipper": float(fr) * 100, "carrier": float(cr) * 100}}


def create_plan(db: Session, order: ShipperOrder, user: Dict[str, Any],
                ip: str = "") -> Dict[str, Any]:
    """生成方案：写 order_plan，版本号自增，订单推进到 planned。

    出方案时查一次路线天气：恶劣天气把 ETA 上浮，并把结论写进 `route_json.weather`，
    这样后面打开方案还能回答"当时为什么算 26 小时而不是 23 小时"。
    """
    from . import weather_risk

    if order.status not in PLANNABLE:
        raise SchedulingError(
            f"订单当前状态「{_label(order.status)}」不能出方案")

    r = estimate_route(order.origin, order.dest)
    if not r.get("ok"):
        raise SchedulingError(f"方案生成失败：{r.get('reason')}")

    dist = Decimal(str(r["distance_km"]))
    base_eta = float(r["eta_h"])
    wp = r.get("waypoints") or [x for x in (order.origin, order.dest) if x]
    risk = weather_risk.assess(wp, order.cargo_type or "普货")
    eta_adj, factor = weather_risk.apply_to_eta(base_eta, risk)
    eta = Decimal(str(eta_adj))
    cost = (dist * DEFAULT_COST_PER_KM).quantize(pricing.CENT)
    ver = db.execute(select(OrderPlan.version).where(
        OrderPlan.order_id == order.id).order_by(
        OrderPlan.version.desc())).scalars().first() or 0

    route_json = {**r, "weather": risk, "eta_base_h": base_eta,
                  "weather_factor": factor}
    plan = OrderPlan(order_id=order.id, version=int(ver) + 1, route_json=route_json,
                     distance_km=dist, eta_h=eta, cost_est=cost,
                     created_by_user_id=user["user_id"])
    db.add(plan)
    db.flush()
    order.active_plan_id = plan.id
    transition(db, order, "planned", "order_planned", "dispatcher", user["user_id"],
               {"plan_id": plan.id, "version": plan.version, "distance_km": float(dist),
                "eta_h": float(eta), "eta_base_h": base_eta,
                "weather_factor": factor, "weather": risk.get("condition"),
                "provider": r.get("provider")})
    db.commit()
    return {"plan_id": plan.id, "version": plan.version,
            "distance_km": float(dist), "eta_h": float(eta),
            "eta_base_h": base_eta, "weather_factor": factor,
            "weather": risk, "cost_est": float(cost),
            "provider": r.get("provider"),
            "waypoints": r.get("waypoints") or [], "status": order.status}


def publish_order(db: Session, order: ShipperOrder, user: Dict[str, Any],
                  gross: Optional[float] = None, ip: str = "") -> Dict[str, Any]:
    """定价挂牌：预算 − 货主侧服务费 = 挂牌价，推送到承运商订单池。"""
    if order.status not in PUBLISHABLE:
        raise SchedulingError(
            f"订单当前状态「{_label(order.status)}」不能发布")
    if not order.active_plan_id:
        raise SchedulingError("还没有方案，请先生成方案")

    amount = Decimal(str(gross if gross else (order.budget_max or 0)))
    if amount <= 0:
        raise SchedulingError("货主未填预算，无法定价；请先让货主补充预算")

    fr, cr = runtime.active_fee(db)
    s = pricing.settle_amounts(amount, fr, cr)
    plan = db.get(OrderPlan, order.active_plan_id)
    warn = ""
    if plan and plan.cost_est and s["publish"] < plan.cost_est:
        warn = f"挂牌价 ¥{s['publish']} 低于估算成本 ¥{plan.cost_est}，可能出现无人接单"

    for old in db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id,
            OrderPublish.status == "open")).scalars().all():
        old.status = "closed"
        old.closed_at = _now()

    pub = OrderPublish(order_id=order.id, plan_id=order.active_plan_id,
                       gross_price=s["gross"], shipper_fee_rate=s["shipper_rate"],
                       shipper_fee=s["shipper_fee"], carrier_fee_rate=s["carrier_rate"],
                       publish_price=s["publish"], status="open",
                       published_by=user["user_id"])
    db.add(pub)
    db.flush()
    transition(db, order, "published", "order_published", "dispatcher", user["user_id"],
               {"publish_id": pub.id, "gross": float(s["gross"]),
                "shipper_fee": float(s["shipper_fee"]),
                "publish_price": float(s["publish"])})
    # 挂牌即开标：承运商看到单子的同时就能报价，不需要调度再点一次"开标"。
    # 同一事务内创建，保证"有 in-pool 的挂牌单却没有招标单"这种半截状态不存在。
    from . import tender_ops

    tender = tender_ops.open_tender(db, order, user, commit=False)
    db.commit()
    return {"publish_id": pub.id, "status": order.status,
            "gross": float(s["gross"]), "shipper_fee": float(s["shipper_fee"]),
            "publish_price": float(s["publish"]),
            "carrier_fee_rate": float(s["carrier_rate"]), "warning": warn,
            "tender_id": tender["tender_id"],
            "tender_deadline_at": tender["deadline_at"]}


def bid_summary(db: Session, order: ShipperOrder) -> Dict[str, Any]:
    """承运商响应情况，用于 Agent 判断"为什么还没人接单"。"""
    from ..models import CarrierBid, SysOrg

    rows = db.execute(select(CarrierBid).where(
        CarrierBid.order_id == order.id)).scalars().all()
    names = {}
    for b in rows:
        if b.carrier_org_id and b.carrier_org_id not in names:
            org = db.get(SysOrg, b.carrier_org_id)
            names[b.carrier_org_id] = org.name if org else str(b.carrier_org_id)
    return {"count": len(rows),
            "carriers": [{"org": names.get(b.carrier_org_id), "status": b.status,
                          "profit": float(b.profit_est) if b.profit_est is not None else None}
                         for b in rows]}


def _label(status: str) -> str:
    from .orders import STATUS_LABEL

    return STATUS_LABEL.get(status, status)


def _now():
    from datetime import datetime

    return datetime.now()


def user_of(db: Session, user_id: int) -> Optional[SysUser]:
    return db.get(SysUser, user_id)


__all__ = ["SchedulingError", "PLANNABLE", "PUBLISHABLE", "preview_plan",
           "create_plan", "publish_order", "bid_summary", "DEFAULT_COST_PER_KM"]
