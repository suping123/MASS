"""承运商端接口。

公共池只给"接单所需"的信息：起讫、货类、重量、里程、运费。
**不含货主预算**——这是三个平台之间最硬的一条信息边界。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agents.carrier_agent import agent as carrier_agent
from ..db import get_db
from ..models import ShipperOrder
from ..schemas import CancelReq, ChatReq, ChatResp
from ..security import require_perm
from ..services import carrier_ops, tender_ops
from . import _gateway as gw

router = APIRouter(prefix="/api/carrier", tags=["carrier"])


class AcceptReq(BaseModel):
    position_text: str = Field(min_length=2, max_length=255,
                               description="接单时的位置（文本，承运商自报）")


class BidReq(BaseModel):
    quote_amount: Optional[float] = Field(
        default=None, gt=0, le=1_000_000,
        description="报出的运费（元）。不传则按成本 + 目标利润率自动出价")
    eta_h: Optional[float] = Field(default=None, gt=0, le=2000,
                                   description="承诺时效（小时）。不传则按方案里程估算")
    note: Optional[str] = Field(default=None, max_length=180, description="报价说明")
    position_text: Optional[str] = Field(default=None, max_length=255,
                                         description="当前所在位置（用于计算空驶）")


class ProfileReq(BaseModel):
    cost_per_km: Optional[float] = Field(default=None, gt=0, le=100)
    capacity_t: Optional[float] = Field(default=None, gt=0, le=200)
    current_position: Optional[str] = Field(default=None, max_length=255)


@router.get("/pool")
def pool(origin: Optional[str] = Query(default=None),
         dest: Optional[str] = Query(default=None),
         limit: int = Query(default=50, ge=1, le=200),
         user: Dict[str, Any] = Depends(require_perm("carrier:list")),
         db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return carrier_ops.pool(db, user.get("org_id"), origin=origin, dest=dest, limit=limit)


@router.post("/chat", response_model=ChatResp)
def chat(req: ChatReq,
         user: Dict[str, Any] = Depends(require_perm("carrier:list")),
         db: Session = Depends(get_db)):
    """对话：查单 / 试算收益 / 接单 / 报异常。接单与报异常都要先复述再确认。"""
    return carrier_agent.handle(db, user, req.text, req.session_id)


@router.post("/orders/{order_id}/quote")
def quote(order_id: int,
          user: Dict[str, Any] = Depends(require_perm("carrier:list")),
          db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return carrier_ops.quote(db, user, order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/orders/{order_id}/accept")
def accept(order_id: int, req: AcceptReq,
           user: Dict[str, Any] = Depends(require_perm("carrier:accept")),
           db: Session = Depends(get_db)) -> Dict[str, Any]:
    """接单：**只投消息**，由承运商 agent 进程执行（S5）。

    超时 504 表示"承运商 agent 没在跑"，409 表示"业务上不行"（如已被接走）。
    两者不能混：前者要去看进程，后者改条件重试就行。
    """
    r = gw.invoke(db, user, action="accept_order",
                  args={"order_id": order_id,
                        "position_text": req.position_text},
                  order_id=order_id)
    return gw.unwrap(r)


@router.post("/orders/{order_id}/reject")
def reject(order_id: int,
           user: Dict[str, Any] = Depends(require_perm("carrier:accept")),
           db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        return carrier_ops.reject(db, user, order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/orders/{order_id}/bid")
def bid(order_id: int, req: BidReq | None = None,
        user: Dict[str, Any] = Depends(require_perm("carrier:accept")),
        db: Session = Depends(get_db)) -> Dict[str, Any]:
    """投标报价：同样只投消息，承运商 agent 进程算出自己的价。

    报价是承运商自己的决定，服务端只做两件事：算成本底线、记账。
    投标**不改变订单状态**——订单由招标裁决推进，不是被浏览推进。
    """
    r = req or BidReq()
    rep = gw.invoke(db, user, action="submit_bid",
                    args={"order_id": order_id,
                          "quote_amount": r.quote_amount,
                          "eta_h": r.eta_h,
                          "note": r.note or "",
                          "position_text": r.position_text or ""},
                    order_id=order_id)
    return gw.unwrap(rep)


@router.get("/orders/{order_id}/tender")
def my_tender(order_id: int,
              user: Dict[str, Any] = Depends(require_perm("carrier:list")),
              db: Session = Depends(get_db)) -> Dict[str, Any]:
    """自己在这单上的投标情况。**看不到别家的报价**，只看得到有几个人在争。"""
    return tender_ops.view(db, order_id, viewer_org=user.get("org_id"))


@router.get("/my-bids")
def my_bids(limit: int = Query(default=50, ge=1, le=200),
            user: Dict[str, Any] = Depends(require_perm("carrier:list")),
            db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    """我的竞价列表：报了多少、第几轮、有没有在跟我谈、结果如何。

    别家的报价不出现，只给"有几家在争"——知道竞争者的价码等于让后报的人
    照着压价，竞价就成了抢尾盘。
    """
    return carrier_ops.my_bids(db, user, limit=limit)


@router.get("/orders")
def my_orders(limit: int = Query(default=100, ge=1, le=300),
              user: Dict[str, Any] = Depends(require_perm("carrier:read_own")),
              db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return carrier_ops.my_orders(db, user, limit=limit)


@router.get("/orders/{order_id}")
def order_detail_api(order_id: int,
                     user: Dict[str, Any] = Depends(require_perm("carrier:read_own")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    """承运商看自己接的单：路线、我的收益构成、事件时间线。

    `include_budget=False` 是三个平台之间最硬的一条信息边界，
    这里**不要**"顺手"打开——承运商能反推货主预算就等于报价失去意义。
    """
    from sqlalchemy import select as _select

    from ..models import CarrierBid, OrderEvent, OrderPlan
    from ..services.orders import order_detail

    o = db.get(ShipperOrder, order_id)
    if not o or o.carrier_org_id != user.get("org_id"):
        raise HTTPException(status_code=404, detail="订单不存在")

    d = order_detail(o, include_budget=False)

    plan = db.get(OrderPlan, o.active_plan_id) if o.active_plan_id else None
    if plan:
        route = plan.route_json or {}
        d["plan"] = {
            "version": plan.version,
            "distance_km": float(plan.distance_km) if plan.distance_km is not None else None,
            "eta_h": float(plan.eta_h) if plan.eta_h is not None else None,
            "provider": route.get("provider"),
            "waypoints": route.get("waypoints") or [],
        }

    bid = db.execute(_select(CarrierBid).where(
        CarrierBid.order_id == order_id,
        CarrierBid.carrier_org_id == user.get("org_id"))).scalars().first()
    if bid:
        def _f(v):
            return float(v) if v is not None else None
        d["bid"] = {"decision": bid.decision,
                    "quoted_price": _f(bid.quoted_price),
                    "est_fee": _f(bid.est_fee),
                    "linehaul_cost": _f(bid.linehaul_cost),
                    "deadhead_km": _f(bid.deadhead_km),
                    "deadhead_cost": _f(bid.deadhead_cost),
                    "deadhead_from": bid.deadhead_from,
                    "est_profit": _f(bid.est_profit),
                    "position_text": bid.position_text,
                    "decided_at": bid.decided_at.isoformat(timespec="seconds")
                    if bid.decided_at else None}

    rows = db.execute(_select(OrderEvent).where(
        OrderEvent.order_id == order_id).order_by(OrderEvent.seq)).scalars().all()
    d["events"] = [{"seq": e.seq, "type": e.event_type, "actor": e.actor_role,
                    "payload": e.payload,
                    "at": e.created_at.isoformat(timespec="seconds") if e.created_at else None}
                   for e in rows]
    return d


@router.post("/orders/{order_id}/cancel")
def cancel_order_api(order_id: int, req: CancelReq, request: Request,
                     user: Dict[str, Any] = Depends(require_perm("order:cancel")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    """承运商取消自己已接的单（放弃承运）。必须填原因，原因进系统日志。"""
    from ..services.orders import cancel_order

    o = db.get(ShipperOrder, order_id)
    # 不是自己接的单：404，不确认订单是否存在
    if not o or o.carrier_org_id != user.get("org_id"):
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        return cancel_order(db, o, user, req.reason, _client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    return (xff.split(",")[0].strip() if xff else
            (request.client.host if request.client else ""))


@router.get("/profile")
def get_profile(user: Dict[str, Any] = Depends(require_perm("carrier:read_own")),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    p = carrier_ops.profile_of(db, user.get("org_id"))
    if not p:
        raise HTTPException(status_code=404, detail="没有承运商档案")
    return {"org_id": p.org_id,
            "cost_per_km": float(p.cost_per_km),
            "capacity_t": float(p.capacity_t),
            "current_position": p.current_position,
            "position_updated_at": p.position_updated_at.isoformat(timespec="seconds")
            if p.position_updated_at else None,
            "rating": float(p.rating), "completed_orders": p.completed_orders}


@router.put("/profile")
def update_profile(req: ProfileReq,
                   user: Dict[str, Any] = Depends(require_perm("carrier:read_own")),
                   db: Session = Depends(get_db)) -> Dict[str, Any]:
    from datetime import datetime

    p = carrier_ops.profile_of(db, user.get("org_id"))
    if not p:
        raise HTTPException(status_code=404, detail="没有承运商档案")
    if req.cost_per_km is not None:
        p.cost_per_km = req.cost_per_km
    if req.capacity_t is not None:
        p.capacity_t = req.capacity_t
    if req.current_position is not None:
        p.current_position = req.current_position
        p.position_updated_at = datetime.now()
    db.commit()
    return {"ok": True, "cost_per_km": float(p.cost_per_km),
            "capacity_t": float(p.capacity_t), "current_position": p.current_position}
