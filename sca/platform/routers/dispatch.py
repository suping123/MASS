"""调度平台接口：出方案 -> 定价挂牌 -> 处置异常。

一条业务硬规则（来自需求）：**非必要不换承运商**。
异常处置的默认动作是改路线/改时间/改承诺，换承运商不走这里：
只有满足 `services/reassign.py` 里枚举的触发条件（拒单、失联超时、资质不符、
车辆报废、人工强制）且经人工审批，才能改派。把它做成 resolve 的一个普通参数，
就等于给系统留了个后门，一旦被误用就是违背货主权益。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (CarrierBid, CarrierProfile, ExceptionReport, OrderEvent,
                      OrderPlan, OrderPublish, ShipperOrder, SysOrg)
from ..schemas import CancelReq, ChatReq
from ..security import require_perm
from ..agents import negotiation
from ..services import pricing, scheduling, tender_ops, triage
from ..services.orders import (STATUS_LABEL, add_event, order_brief,
                               order_detail, transition)
from ..services.routing import estimate_route
from . import _gateway as gw

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

DEFAULT_COST_PER_KM = Decimal("2.60")


class PublishReq(BaseModel):
    gross: Optional[float] = Field(default=None, description="不传则用货主预算")
    note: Optional[str] = None


@router.get("/agents/thread")
def agents_thread(order_id: Optional[int] = Query(default=None),
                  exception_id: Optional[int] = Query(default=None),
                  user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                  db: Session = Depends(get_db)) -> Dict[str, Any]:
    """回放一笔业务上 Agent 之间的全部消息（含被拒绝的），用于排查与演示。"""
    from ..agents import bus

    if not order_id and not exception_id:
        raise HTTPException(status_code=400, detail="需给出 order_id 或 exception_id")
    return bus.thread_view(db, order_id=order_id, exception_id=exception_id)


class ResolveReq(BaseModel):
    resolution: str = Field(min_length=2, max_length=2000)
    action: str = Field(default="resume",
                        pattern="^(resume|replan|extend|deliver|reassign)$")
    # extend 专用：延后多少小时（0.5–72）。只能延后，提前属改单。
    delay_h: Optional[float] = Field(default=None, description="改期时长（小时）")
    # reassign 专用：触发码与新承运商（不传则退回公开池重新挂牌）
    reason_code: Optional[str] = Field(default=None, description="改派触发码")
    new_carrier_org_id: Optional[int] = Field(default=None, description="指定新承运商")


class ReroutePreviewReq(BaseModel):
    """改道预览入参。不传描述就用异常单自身的位置与描述。"""
    avoid_city: Optional[str] = Field(default=None, description="指定受影响城市")
    description: Optional[str] = Field(default=None, description="异常描述")


@router.get("/orders")
def list_orders(status: Optional[str] = Query(default=None),
                limit: int = Query(default=100, ge=1, le=500),
                user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(ShipperOrder)
    if status:
        q = q.where(ShipperOrder.status == status)
    else:
        q = q.where(ShipperOrder.status.notin_(("draft",)))
    q = q.order_by(ShipperOrder.id.desc()).limit(limit)
    out = []
    for o in db.execute(q).scalars().all():
        d = order_brief(o)
        d["budget_max"] = float(o.budget_max) if o.budget_max is not None else None
        d["shipper_org"] = _org_name(db, o.shipper_org_id)
        out.append(d)
    return out


@router.get("/agents/status")
def agents_status(user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                  db: Session = Depends(get_db)) -> Dict[str, Any]:
    """四个 Agent 进程的在线状态。

    页面必须能回答"为什么挂牌点了没反应"：是单子不合法（409），
    还是 agent 没起来（504）。只把心跳与积压摆出来，不替人做判断。
    """
    from ..agents import bus
    from ..models import AgentActor

    rows = db.execute(select(AgentActor).order_by(AgentActor.agent_key)).scalars().all()
    names = {o.id: o.name for o in db.execute(select(SysOrg)).scalars().all()}
    out = []
    orphans = []
    for a in rows:
        kind = a.kind or str(a.agent_key).split(":")[0]
        # 组织已经不在库里（测试造过又删掉、或组织被撤）的注册行是残留：
        # 它确实有心跳（worker 还在为这个 key 兜底），但背后没有承运商/货主，
        # 显示成"在线"会让人去查一个不存在的机构。
        # 注意 ``shipper`` / ``dispatch`` / ``exception`` 是单例，本来就**没有** org_id，
        # 只有"登记了 org 但那个机构没了"才算孤儿。
        if a.org_id and kind in ("shipper", "carrier") and not names.get(a.org_id):
            orphans.append(a.agent_key)
            continue
        age = (datetime.now() - a.last_seen_at).total_seconds() if a.last_seen_at else None
        org = names.get(a.org_id) if a.org_id else None
        label = {"dispatch": "调度", "exception": "异常域",
                 "shipper": "货主", "carrier": "承运商"}.get(kind, kind)
        out.append({
            "agent_key": a.agent_key, "kind": kind, "org_id": a.org_id,
            "name": (f"{org} {label}" if org else label),
            "online": bool(age is not None and age <= bus.ALIVE_STALE_S),
            "age_s": round(age, 1) if age is not None else None,
            "pid": a.pid, "host": a.host,
            "last_seen_at": (a.last_seen_at.isoformat(timespec="seconds")
                             if a.last_seen_at else None),
            "pending": bus.pending_count(db, a.agent_key),
        })
    return {"agents": out, "stale_s": bus.ALIVE_STALE_S,
            "orphans": orphans,
            "offline": [x["agent_key"] for x in out if not x["online"]],
            "any_offline": any(not x["online"] for x in out)}


@router.get("/tenders")
def tender_board(status: str = Query(default="active",
                                     description="active=进行中 / all / open / negotiating / awarded / failed"),
                 limit: int = Query(default=50, ge=1, le=200),
                 user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    """竞价看板：跨单的招标摘要（几家在报、报到什么价、什么时候截止）。

    单票的裁决证据仍在 ``/orders/{id}/tender``，这里只给一行能扫的信息——
    列表接口塞几十 KB 证据表，页面会先卡在渲染上。
    """
    return tender_ops.board(db, status=status, limit=limit)


@router.get("/orders/{order_id}")
def order_detail_api(order_id: int,
                     user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    o = db.get(ShipperOrder, order_id)
    if not o:
        raise HTTPException(status_code=404, detail="订单不存在")
    d = order_detail(o, include_budget=True)
    d["shipper_org"] = _org_name(db, o.shipper_org_id)
    plan = db.get(OrderPlan, o.active_plan_id) if o.active_plan_id else None
    if plan:
        route = plan.route_json or {}
        d["plan"] = {"id": plan.id, "version": plan.version,
                     "distance_km": float(plan.distance_km) if plan.distance_km else None,
                     "eta_h": float(plan.eta_h) if plan.eta_h else None,
                     "cost_est": float(plan.cost_est) if plan.cost_est else None,
                     "provider": route.get("provider"),
                     "waypoints": route.get("waypoints") or [],
                     # 天气只上浮 ETA：把基准值与系数一起给出去，页面才能解释
                     # "为什么是 40 小时而不是 36 小时"，而不是让调度去猜。
                     "eta_base_h": route.get("eta_base_h"),
                     "weather_factor": route.get("weather_factor"),
                     "weather": route.get("weather"),
                     "avoid_city": route.get("avoid_city"),
                     "avoid_verified": route.get("avoid_verified"),
                     "real_note": route.get("real_note"),
                     "previous": route.get("previous")}
    pub = db.execute(select(OrderPublish).where(
        OrderPublish.order_id == order_id).order_by(OrderPublish.id.desc())).scalars().first()
    if pub:
        d["publish"] = {"id": pub.id, "status": pub.status,
                        "gross_price": float(pub.gross_price),
                        "shipper_fee": float(pub.shipper_fee),
                        "publish_price": float(pub.publish_price),
                        "carrier_fee_rate": float(pub.carrier_fee_rate)}
    d["bids"] = [{"carrier_org": _org_name(db, b.carrier_org_id),
                  "decision": b.decision,
                  # 报价是承运商自己给的；quoted_price 是服务端按挂牌价回填的旧口径
                  "quote": float(b.quote_amount) if b.quote_amount is not None else None,
                  "eta_h": float(b.quote_eta_h) if b.quote_eta_h is not None else None,
                  "round": b.bid_round,
                  "note": b.bid_note,
                  "profit": float(b.est_profit) if b.est_profit is not None else None}
                 for b in db.execute(select(CarrierBid).where(
                     CarrierBid.order_id == order_id)).scalars().all()]
    # 招标进度与裁决证据：详情里能直接看到"为什么是这家"
    tender = tender_ops.view(db, order_id)
    d["tender"] = tender if tender.get("ok") else None
    d["events"] = _events(db, order_id)
    return d


@router.post("/orders/{order_id}/plan")
def make_plan(order_id: int,
              user: Dict[str, Any] = Depends(require_perm("dispatch:plan")),
              db: Session = Depends(get_db)) -> Dict[str, Any]:
    """生成调度方案。真正的实现在 services/scheduling.py，Agent 与页面共用。"""
    o = _get(db, order_id)
    try:
        r = scheduling.create_plan(db, o, user)
    except scheduling.SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"plan": {"id": r["plan_id"], "version": r["version"],
                     "distance_km": r["distance_km"], "eta_h": r["eta_h"],
                     "cost_est": r["cost_est"], "provider": r.get("provider")},
            "status": r["status"]}


@router.post("/orders/{order_id}/publish")
def publish(order_id: int, req: PublishReq | None = None,
            user: Dict[str, Any] = Depends(require_perm("dispatch:publish")),
            db: Session = Depends(get_db)) -> Dict[str, Any]:
    """定价并挂牌到承运商公共订单池。

    两侧各扣 2%，承运商只看到扣完货主侧服务费的价。执行者是**调度 agent 进程**
    （S5）：这里只投消息并等回复，挂牌之后承运商才能投标，这一环断掉后面全断。
    """
    _get(db, order_id)          # 订单不存在仍然是 404，不该让 agent 去回答"不存在"
    r = gw.invoke(db, user, action="publish_order",
                  args={"order_id": order_id,
                        "gross": (req.gross if req else None)},
                  order_id=order_id)
    return gw.unwrap(r)


@router.get("/orders/{order_id}/tender")
def get_tender(order_id: int,
               user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    """招标进度与裁决证据。调度看得到全部报价与每家被淘汰的原因。"""
    _get(db, order_id)
    return tender_ops.view(db, order_id)


@router.post("/orders/{order_id}/tender/open")
def open_tender(order_id: int,
                minutes: int = Query(default=30, ge=1, le=1440),
                user: Dict[str, Any] = Depends(require_perm("dispatch:publish")),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    """提前开标 / 重新开标。挂牌时已自动开标，这里用于人工延长或重开。"""
    o = _get(db, order_id)
    try:
        return tender_ops.open_tender(db, o, user, minutes=minutes)
    except tender_ops.TenderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/orders/{order_id}/tender/close")
def close_tender(order_id: int,
                 force: bool = Query(default=False, description="提前截止并立即裁决"),
                 negotiate: bool = Query(default=True,
                                         description="裁决结果为可谈时自动发起还价"),
                 user: Dict[str, Any] = Depends(require_perm("dispatch:publish")),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    """收标截止并裁决。截止时间未到且没 force 时只回报"还没到点"。

    裁决落在 ``negotiating`` 时默认顺手把第一轮还价发出去——"到期了还有人
    超预算但差得不多"本来就要接着谈，让人再点一次没有意义。
    """
    o = _get(db, order_id)
    try:
        r = tender_ops.close_tender(db, o, user, force=force)
    except tender_ops.TenderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if negotiate and r.get("status") == "negotiating":
        t = tender_ops.active_tender(db, order_id)
        if t:
            r["negotiation_run"] = negotiation.advance(db, o, t, user=user)
            r["status"] = r["negotiation_run"].get("status", r["status"])
            r["status_label"] = r["negotiation_run"].get("status_label",
                                                         r.get("status_label"))
    return r


@router.post("/orders/{order_id}/tender/negotiate")
def negotiate_round(order_id: int,
                    user: Dict[str, Any] = Depends(require_perm("dispatch:publish")),
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    """把本单的还价往前推一轮（S4 之后由 dispatch daemon 自动做）。

    这里保留为**人工催办**：正常路径是调度 agent 的 `due()`/收到 counter 后自己谈，
    这个接口只给"我已经看到它停了、想手动推一下"的场景。它不投消息，
    因为谈判的两端都是 agent（dispatch ↔ carrier），人插进去只能打断对话树。
    """
    o = _get(db, order_id)
    t = tender_ops.active_tender(db, order_id)
    if t is None:
        raise HTTPException(status_code=409, detail="这单没有进行中的招标")
    try:
        return negotiation.advance(db, o, t, user=user)
    except tender_ops.TenderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/exceptions")
def list_exceptions(status: Optional[str] = Query(default="open"),
                    user: Dict[str, Any] = Depends(require_perm("dispatch:exception")),
                    db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(ExceptionReport)
    if status:
        q = q.where(ExceptionReport.status == status)
    rows = db.execute(q.order_by(ExceptionReport.id.desc()).limit(100)).scalars().all()
    out = []
    for e in rows:
        o = db.get(ShipperOrder, e.order_id)
        out.append({"id": e.id, "order_id": e.order_id,
                    "order_no": o.order_no if o else None,
                    "position_text": e.position_text,
                    "code": e.exception_code, "description": e.description,
                    "status": e.status,
                    "reported_at": e.created_at.isoformat(timespec="seconds") if e.created_at else None})
    return out


@router.post("/orders/{order_id}/cancel")
def cancel_order_api(order_id: int, req: CancelReq, request: Request,
                     user: Dict[str, Any] = Depends(require_perm("order:cancel")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    """调度员取消订单（任何未完成的单）。必须填原因。"""
    from ..services.orders import cancel_order

    o = _get(db, order_id)
    try:
        return cancel_order(db, o, user, req.reason, _client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


class RollbackReq(BaseModel):
    target_status: str = Field(description="只允许退回上一个业务节点")
    reason: str = Field(min_length=2, max_length=500)


@router.post("/orders/{order_id}/rollback")
def rollback_order_api(order_id: int, req: RollbackReq, request: Request,
                       user: Dict[str, Any] = Depends(require_perm("dispatch:history")),
                       db: Session = Depends(get_db)) -> Dict[str, Any]:
    """回退订单（审核驳回 / 高危审批驳回后的返工入口）。只允许退回上一节点。"""
    from ..services.orders import rollback_order

    o = _get(db, order_id)
    try:
        return rollback_order(db, o, user, req.target_status, req.reason,
                              _client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/history")
def history(status: Optional[str] = Query(default=None),
            keyword: Optional[str] = Query(default=None),
            limit: int = Query(default=100, ge=1, le=500),
            user: Dict[str, Any] = Depends(require_perm("dispatch:history")),
            db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    """历史调度：默认返回已结束的单（送达/结算/取消/驳回），也支持按状态精查。"""
    from ..services.orders import ROLLBACK, STATUS_LABEL

    q = select(ShipperOrder)
    if status:
        q = q.where(ShipperOrder.status == status)
    else:
        q = q.where(ShipperOrder.status.in_(("delivered", "settled", "cancelled")))
    if keyword:
        like = f"%{keyword}%"
        q = q.where(ShipperOrder.order_no.like(like) | ShipperOrder.origin.like(like)
                    | ShipperOrder.dest.like(like))
    rows = db.execute(q.order_by(ShipperOrder.id.desc()).limit(limit)).scalars().all()
    out = []
    for o in rows:
        d = order_brief(o)
        d["shipper_org"] = _org_name(db, o.shipper_org_id)
        d["carrier_org"] = _org_name(db, o.carrier_org_id)
        d["can_rollback"] = sorted(ROLLBACK.get(o.status, set()))
        d["last_event"] = _last_event(db, o.id)
        out.append(d)
    return out


def _last_event(db: Session, order_id: int) -> Optional[Dict[str, Any]]:
    e = db.execute(select(OrderEvent).where(
        OrderEvent.order_id == order_id).order_by(OrderEvent.seq.desc())
    ).scalars().first()
    if not e:
        return None
    return {"seq": e.seq, "type": e.event_type, "actor": e.actor_role,
            "payload": e.payload,
            "at": e.created_at.isoformat(timespec="seconds") if e.created_at else None}


@router.get("/audit")
def audit_logs(action: Optional[str] = Query(default=None),
               target_type: Optional[str] = Query(default=None),
               actor_role: Optional[str] = Query(default=None),
               keyword: Optional[str] = Query(default=None),
               limit: int = Query(default=100, ge=1, le=500),
               user: Dict[str, Any] = Depends(require_perm("dispatch:audit_read")),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    """系统日志查询。调度员可读；只有超管能"操作"（README 与权限表里有说明）。"""
    from ..services import audit

    return {"items": audit.query(db, action=action, target_type=target_type,
                                 actor_role=actor_role, keyword=keyword, limit=limit),
            "actions": audit.distinct_actions(db)}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    return (xff.split(",")[0].strip() if xff else
            (request.client.host if request.client else ""))


@router.get("/exceptions/{eid}/sop")
def suggest_sop(eid: int,
                user: Dict[str, Any] = Depends(require_perm("dispatch:exception")),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    """按上报内容做分级 + 检索处置 SOP，给调度员一个起点。

    这只是**建议**：处置结论仍由调度员填写。系统不会因为检索到 SOP 就自动
    执行处置动作——异常处置涉及改路线、改时效，属不可逆的外部影响。
    """
    e = db.get(ExceptionReport, eid)
    if not e:
        raise HTTPException(status_code=404, detail="异常记录不存在")
    o = db.get(ShipperOrder, e.order_id)
    r = triage.suggest(e.description or "", o)
    return {"exception_id": eid, "query": e.description,
            "triage": r["triage"],
            "channel": (r["sop"][0].get("channel") if r["sop"] else None),
            "hits": r["sop"]}


@router.post("/exceptions/{eid}/reroute-preview")
def reroute_preview(eid: int, req: ReroutePreviewReq,
                    user: Dict[str, Any] = Depends(require_perm("dispatch:exception")),
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    """改道预览：算一份对比方案但**不落库**，调度确认后才走 resolve(action=replan)。

    单独做成一个接口，是因为改道会真的重算里程/时效/成本，调度需要先看到
    "绕开哪个城市、多跑多少公里、晚到几小时"再决定，而不是提交后才知道。
    """
    from ..services import reroute

    e = db.get(ExceptionReport, eid)
    if not e:
        raise HTTPException(status_code=404, detail="异常记录不存在")
    order = db.get(ShipperOrder, e.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return reroute.preview_reroute(
        db, order, position_text=e.position_text or "",
        description=req.description or e.description or "",
        avoid_city=req.avoid_city or "")


@router.post("/exceptions/{eid}/resolve")
def resolve_exception(eid: int, req: ResolveReq,
                      request: Request,
                      user: Dict[str, Any] = Depends(require_perm("dispatch:exception")),
                      db: Session = Depends(get_db)) -> Dict[str, Any]:
    """处置异常。改派要满足触发条件并走独立审批，不从这里夹带。"""
    e = db.get(ExceptionReport, eid)
    if not e:
        raise HTTPException(status_code=404, detail="异常记录不存在")
    try:
        return triage.resolve(db, e, user, req.action, req.resolution,
                              ip=_client_ip(request), delay_h=req.delay_h,
                              reason_code=req.reason_code,
                              new_carrier_org_id=req.new_carrier_org_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/weather/scan")
def weather_scan(dry_run: bool = Query(default=True, description="只预览不落库"),
                 limit: int = Query(default=20, ge=1, le=100),
                 user: Dict[str, Any] = Depends(require_perm("dispatch:exception")),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    """扫描在途订单路线天气，恶劣天气自动生成异常单。

    默认 `dry_run=true` 只预览：调度先看"有哪几单会被立异常"，确认后再执行。
    这类批量写操作没有预览就直接建单，等于让外部天气服务决定系统里多出多少待办。
    """
    from ..services import weather_watch

    return weather_watch.scan_inflight(db, limit=limit, dry_run=dry_run)


@router.get("/reassign/options")
def reassign_options(order_id: int = Query(gt=0),
                     user: Dict[str, Any] = Depends(require_perm("dispatch:reassign")),
                     db: Session = Depends(get_db)) -> Dict[str, Any]:
    """改派选项：每个触发码对这笔单是否成立、可选的新承运商。

    前端拿这个渲染弹窗，不再自己猜哪个条件满足——判定只有一处（`reassign.check_eligible`）。
    """
    from ..services import reassign as ra

    order = db.get(ShipperOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    triggers = []
    for code, meta in ra.REASSIGN_TRIGGERS.items():
        ok, why = ra.check_eligible(db, order, code)
        triggers.append({"code": code, "label": meta["label"],
                         "detail": meta["detail"], "eligible": ok, "why": why,
                         "min_reason": meta.get("min_reason", 10)})

    rows = db.execute(select(CarrierProfile, SysOrg).join(
        SysOrg, SysOrg.id == CarrierProfile.org_id)).all()
    carriers = [{"org_id": p.org_id, "name": o.name,
                 "cost_per_km": float(p.cost_per_km or 0),
                 "capacity_t": float(p.capacity_t or 0),
                 "rating": float(p.rating or 0),
                 "current": p.org_id == order.carrier_org_id} for p, o in rows]

    return {"order_id": order.id, "order_no": order.order_no,
            "status": order.status, "status_label": STATUS_LABEL.get(order.status),
            "carrier_org_id": order.carrier_org_id,
            "triggers": triggers, "carriers": carriers}


@router.post("/chat")
def chat(req: ChatReq,
         user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
         db: Session = Depends(get_db)) -> Dict[str, Any]:
    """智能调度助手：对话式出方案/挂牌/处置异常，也能回答订单与货类问题。

    与页面按钮调的是同一套 services（scheduling / triage），所以"Agent 说的价"
    和"按钮算的价"不会出现两个口径。
    """
    from ..agents.dispatch_agent import agent as dispatch_agent

    return dispatch_agent.handle(db, user, req.text, req.session_id)


@router.get("/chat/{session_id}")
def chat_history(session_id: str,
                 user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
                 db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    from ..models import ChatMessage

    rows = db.execute(select(ChatMessage).where(
        ChatMessage.session_id == session_id,
        ChatMessage.user_id == user["user_id"],
    ).order_by(ChatMessage.id)).scalars().all()
    return [{"role": m.role, "content": m.content,
             "meta": m.meta,
             "at": m.created_at.isoformat(timespec="seconds") if m.created_at else None}
            for m in rows]


@router.get("/overview")
def overview(user: Dict[str, Any] = Depends(require_perm("dispatch:read_all")),
             db: Session = Depends(get_db)) -> Dict[str, Any]:
    """调度台顶部概览：各状态订单数 + 待处置异常（按等级排序）。"""
    from collections import Counter

    rows = db.execute(select(ShipperOrder.status, ShipperOrder.id)).all()
    cnt = Counter(s for s, _ in rows)
    return {"orders": dict(cnt), "total": len(rows),
            "open_exceptions": triage.open_exceptions(db, limit=10)}


def _get(db: Session, oid: int) -> ShipperOrder:
    o = db.get(ShipperOrder, oid)
    if not o:
        raise HTTPException(status_code=404, detail="订单不存在")
    return o


def _org_name(db: Session, org_id: Optional[int]) -> Optional[str]:
    if not org_id:
        return None
    org = db.get(SysOrg, org_id)
    return org.name if org else None


def _events(db: Session, order_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(select(OrderEvent).where(
        OrderEvent.order_id == order_id).order_by(OrderEvent.seq)).scalars().all()
    return [{"seq": e.seq, "type": e.event_type, "actor": e.actor_role,
             "payload": e.payload,
             "at": e.created_at.isoformat(timespec="seconds") if e.created_at else None}
            for e in rows]
