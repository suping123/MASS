"""货主端接口。

数据隔离原则：所有查询都强制带 `shipper_user_id = 当前用户`，
不接受前端传 user_id —— 传了也不认。这是"货主只能看自己的单"的唯一实现点。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import shipper_agent
from ..db import get_db
from ..models import (ChatMessage, OrderEvent, OrderPlan, OrderPublish, ShipperOrder,
                      SysOrg)
from ..schemas import CancelReq, ChatReq, ChatResp
from ..security import require_perm
from ..services.orders import STATUS_LABEL, order_brief, order_detail

router = APIRouter(prefix="/api/shipper", tags=["shipper"])


@router.post("/chat", response_model=ChatResp)
def chat(req: ChatReq, user: Dict[str, Any] = Depends(require_perm("order:create")),
         db: Session = Depends(get_db)):
    """对话式下单：缺信息会反问，齐全后先复述再确认，确认才落单。"""
    return shipper_agent.handle(db, user, req.text, req.session_id)


@router.get("/orders")
def my_orders(status: Optional[str] = Query(default=None),
              limit: int = Query(default=50, ge=1, le=200),
              user: Dict[str, Any] = Depends(require_perm("order:read_own")),
              db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(ShipperOrder).where(ShipperOrder.shipper_user_id == user["user_id"])
    if status:
        q = q.where(ShipperOrder.status == status)
    q = q.order_by(ShipperOrder.id.desc()).limit(limit)
    return [order_brief(o) for o in db.execute(q).scalars().all()]


@router.get("/orders/{order_id}")
def order_detail_api(order_id: int,
                     user: Dict[str, Any] = Depends(require_perm("order:read_own")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    o = db.get(ShipperOrder, order_id)
    # 不是自己的单：返回 404 而不是 403 —— 不确认订单是否存在，避免枚举
    if not o or o.shipper_user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    d = order_detail(o, include_budget=True)

    # 货主侧自己那笔账：预算 -> 平台服务费 -> 实际承运价。都是他付出去的钱，
    # 可以看；承运商的成本与收益不属于他的信息，这里不返回。
    pub = db.execute(select(OrderPublish).where(
        OrderPublish.order_id == o.id).order_by(OrderPublish.id.desc())).scalars().first()
    if pub:
        d["publish"] = {"status": pub.status,
                        "gross_price": float(pub.gross_price),
                        "shipper_fee": float(pub.shipper_fee),
                        "publish_price": float(pub.publish_price)}

    if o.carrier_org_id:
        org = db.get(SysOrg, o.carrier_org_id)
        d["carrier_org"] = org.name if org else None

    plan = db.get(OrderPlan, o.active_plan_id) if o.active_plan_id else None
    if plan:
        route = plan.route_json or {}
        d["plan"] = {"version": plan.version,
                     "distance_km": float(plan.distance_km) if plan.distance_km is not None else None,
                     "eta_h": float(plan.eta_h) if plan.eta_h is not None else None,
                     "waypoints": route.get("waypoints") or []}

    d["events"] = _events(db, o.id)
    return d


@router.get("/orders/{order_id}/events")
def order_events(order_id: int,
                 user: Dict[str, Any] = Depends(require_perm("order:read_own")),
                 db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    o = db.get(ShipperOrder, order_id)
    if not o or o.shipper_user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    return _events(db, o.id)


@router.get("/chat/{session_id}")
def chat_history(session_id: str,
                 user: Dict[str, Any] = Depends(require_perm("order:create")),
                 db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = db.execute(select(ChatMessage).where(
        ChatMessage.session_id == session_id,
        ChatMessage.user_id == user["user_id"],
    ).order_by(ChatMessage.id)).scalars().all()
    return [{"role": m.role, "content": m.content,
             "at": m.created_at.isoformat(timespec="seconds") if m.created_at else None}
            for m in rows]


@router.post("/orders/{order_id}/cancel")
def cancel_order_api(order_id: int, req: CancelReq,
                     request: Request,
                     user: Dict[str, Any] = Depends(require_perm("order:cancel")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    """货主取消自己的订单。必须填原因，原因进系统日志。"""
    from ..services.orders import cancel_order

    o = db.get(ShipperOrder, order_id)
    # 不是自己的单：404 而不是 403，避免被用来枚举别人的订单号
    if not o or o.shipper_user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        return cancel_order(db, o, user, req.reason, _ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    return (xff.split(",")[0].strip() if xff else
            (request.client.host if request.client else ""))


def _events(db: Session, order_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(select(OrderEvent).where(
        OrderEvent.order_id == order_id).order_by(OrderEvent.seq)).scalars().all()
    return [{"seq": e.seq, "type": e.event_type, "actor": e.actor_role,
             "payload": e.payload,
             "at": e.created_at.isoformat(timespec="seconds") if e.created_at else None}
            for e in rows]
