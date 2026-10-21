"""调度平台的系统管理接口（配置类 + 组织类）。

权限分层（严格按 RBAC，不靠前端隐藏按钮）：
  * 查看：dispatch:fee_rule_view / dispatch:org_view / admin:model_config ...
  * 变更：admin:* 系列，调度员一律 403。
所有变更写 `sys_audit_log`，并且**敏感值只回掩码**——页面永远拿不到明文密钥。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (CarrierProfile, FeeRule, ShipperOrder, SysOrg, SysUser,
                      SystemConfig)
from ..security import hash_password, require_perm
from ..services import audit, runtime

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ------------------------------------------------------------------ 服务费规则
class FeeRuleReq(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    shipper_rate: float = Field(ge=0, le=0.5, description="0.02 = 2%")
    carrier_rate: float = Field(ge=0, le=0.5)
    note: Optional[str] = Field(default=None, max_length=200)


@router.get("/fee-rules")
def list_fee_rules(user: Dict[str, Any] = Depends(require_perm("dispatch:fee_rule_view")),
                   db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = db.execute(select(FeeRule).order_by(FeeRule.id.desc())).scalars().all()
    active = next((r for r in rows if r.status == "active"), None)
    return {
        "active": _fee_dict(active) if active else None,
        "items": [_fee_dict(r) for r in rows],
        "can_edit": "admin:fee_rule" in user["perms"],
    }


@router.post("/fee-rules")
def create_fee_rule(req: FeeRuleReq,
                    user: Dict[str, Any] = Depends(require_perm("admin:fee_rule")),
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    """新建一条规则并置为生效；旧规则归档（同一时刻只允许一条 active）。"""
    if req.shipper_rate + req.carrier_rate >= 0.5:
        raise HTTPException(status_code=422, detail="两侧费率之和过大（>=50%），请核对")
    from datetime import datetime

    for old in db.execute(select(FeeRule).where(FeeRule.status == "active")).scalars().all():
        old.status = "archived"
    row = FeeRule(name=req.name.strip(), shipper_rate=Decimal(str(req.shipper_rate)),
                  carrier_rate=Decimal(str(req.carrier_rate)), status="active",
                  effective_from=datetime.now(), note=req.note, created_by=user["user_id"])
    db.add(row)
    db.flush()
    audit.log(db, user, "fee_rule_changed", target_type="fee_rule", target_id=row.id,
              reason=f"新规则：{req.name}",
              detail={"shipper_rate": req.shipper_rate, "carrier_rate": req.carrier_rate})
    db.commit()
    runtime.invalidate()
    return {"ok": True, "rule": _fee_dict(row)}


def _fee_dict(r: FeeRule) -> Dict[str, Any]:
    return {"id": r.id, "name": r.name,
            "shipper_rate": float(r.shipper_rate), "carrier_rate": float(r.carrier_rate),
            "status": r.status, "note": r.note,
            "effective_from": r.effective_from.isoformat(timespec="seconds")
            if r.effective_from else None}


# ------------------------------------------------------------------ 系统配置
class ConfigReq(BaseModel):
    value: Any = Field(description="JSON 值，整体覆盖")


@router.get("/config")
def get_config(category: Optional[str] = Query(default=None),
               user: Dict[str, Any] = Depends(require_perm("admin:model_config")),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    q = select(SystemConfig)
    if category:
        q = q.where(SystemConfig.category == category)
    rows = db.execute(q.order_by(SystemConfig.category, SystemConfig.cfg_key)).scalars().all()
    return {"items": [{"key": r.cfg_key, "value": r.cfg_value, "category": r.category,
                       "note": r.note,
                       "updated_at": r.updated_at.isoformat(timespec="seconds")
                       if r.updated_at else None} for r in rows],
            "effective_fee": [str(x) for x in runtime.active_fee(db)]}


@router.put("/config/{key}")
def put_config(key: str, req: ConfigReq,
               user: Dict[str, Any] = Depends(require_perm("admin:model_config")),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    row = db.get(SystemConfig, key)
    if not row:
        raise HTTPException(status_code=404, detail=f"配置项不存在：{key}")
    old = row.cfg_value
    row.cfg_value = req.value
    row.updated_by = user["user_id"]
    audit.log(db, user, "config_changed", target_type="config", target_id=key,
              reason="系统配置变更", detail={"old": old, "new": req.value})
    db.commit()
    runtime.invalidate()
    # 配置写库只是第一步，必须同步到进程内的开关容器，
    # 否则"降级工厂"改了等于没改（内核读的是 runtime_flags，不是 DB）。
    if key == "model.enabled":
        from sca import runtime_flags

        runtime_flags.apply_from_config(req.value if isinstance(req.value, dict) else {},
                                        source=f"user:{user['account']}")
    return {"ok": True, "key": key, "value": row.cfg_value,
            "note": "已立即生效（配置读取有 30 秒缓存，此处已主动失效）"}


# ------------------------------------------------------------------ MCP / 模型凭据
class CredReq(BaseModel):
    kind: str = Field(pattern="^(model|tool)$")
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=64)
    service: Optional[str] = None
    enabled: bool = True
    data: Dict[str, str] = Field(default_factory=dict,
                                 description="留空字符串表示不修改既有密钥")


@router.get("/credentials")
def list_credentials(kind: str = Query(default="tool", pattern="^(model|tool)$"),
                     user: Dict[str, Any] = Depends(require_perm("admin:mcp_key"))
                     ) -> Dict[str, Any]:
    """只返回掩码。页面永远拿不到明文密钥——这是硬约束，不是 UI 约定。"""
    import sca.credentials as cred

    k = cred.KIND_MODEL if kind == "model" else cred.KIND_TOOL
    return {"items": cred.list_credentials(k), "summary": cred.summary(),
            "services": list(cred.TOOL_SERVICES.keys()),
            "model_fields": list(cred.MODEL_FIELDS),
            "tool_fields": {k2: list(v) for k2, v in cred.TOOL_SERVICES.items()}}


@router.post("/credentials")
def upsert_credential(req: CredReq,
                      user: Dict[str, Any] = Depends(require_perm("admin:mcp_key"))
                      ) -> Dict[str, Any]:
    import sca.credentials as cred

    k = cred.KIND_MODEL if req.kind == "model" else cred.KIND_TOOL
    res = cred.upsert_credential(k, {"id": req.id, "name": req.name,
                                     "service": req.service, "enabled": req.enabled,
                                     "data": req.data})
    db = _session()
    try:
        audit.log(db, user, "credential_changed", target_type="credential",
                  target_id=res.get("id") or req.name,
                  reason=f"{req.kind}/{req.service or 'llm'} 凭据变更",
                  detail={"service": req.service, "name": req.name,
                          "fields": sorted(set(req.data) & set(req.data))})
        db.commit()
    finally:
        db.close()
    return res


@router.delete("/credentials/{kind}/{cid}")
def delete_credential(kind: str, cid: str,
                      user: Dict[str, Any] = Depends(require_perm("admin:mcp_key"))) -> Dict[str, Any]:
    import sca.credentials as cred

    if kind not in ("model", "tool"):
        raise HTTPException(status_code=422, detail="kind 只能是 model / tool")
    k = cred.KIND_MODEL if kind == "model" else cred.KIND_TOOL
    res = cred.delete_credential(k, cid)
    db = _session()
    try:
        audit.log(db, user, "credential_deleted", target_type="credential", target_id=cid,
                  reason="删除凭据")
        db.commit()
    finally:
        db.close()
    return res


# ------------------------------------------------------------------ 组织 / 用户
@router.get("/orgs")
def list_orgs(org_type: Optional[str] = Query(default=None, pattern="^(shipper|carrier|platform)$"),
              user: Dict[str, Any] = Depends(require_perm("dispatch:org_view")),
              db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(SysOrg)
    if org_type:
        q = q.where(SysOrg.org_type == org_type)
    out = []
    for o in db.execute(q.order_by(SysOrg.id)).scalars().all():
        users = db.execute(select(SysUser).where(SysUser.org_id == o.id)).scalars().all()
        orders = db.execute(select(ShipperOrder.id).where(
            ShipperOrder.carrier_org_id == o.id)).all() if o.org_type == "carrier" else []
        item = {"id": o.id, "name": o.name, "type": o.org_type, "status": o.status,
                "contact_name": o.contact_name, "contact_phone": o.contact_phone,
                "users": [{"id": u.id, "account": u.account, "name": u.display_name,
                           "role": u.role, "status": u.status} for u in users],
                "order_count": len(orders)}
        if o.org_type == "carrier":
            p = db.execute(select(CarrierProfile).where(
                CarrierProfile.org_id == o.id)).scalars().first()
            item["profile"] = {"cost_per_km": float(p.cost_per_km) if p else None,
                               "capacity_t": float(p.capacity_t) if p else None,
                               "current_position": p.current_position if p else None,
                               "completed_orders": p.completed_orders if p else 0}
        out.append(item)
    return out


class OrgReq(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    contact_name: Optional[str] = Field(default=None, max_length=64)
    contact_phone: Optional[str] = Field(default=None, max_length=32)
    status: Optional[int] = Field(default=None, ge=0, le=1)


@router.put("/orgs/{org_id}")
def update_org(org_id: int, req: OrgReq,
               user: Dict[str, Any] = Depends(require_perm("admin:org_manage")),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    o = db.get(SysOrg, org_id)
    if not o:
        raise HTTPException(status_code=404, detail="组织不存在")
    if req.name and req.name.strip() != o.name:
        if db.execute(select(SysOrg.id).where(SysOrg.name == req.name.strip())).first():
            raise HTTPException(status_code=409, detail="组织名已存在")
        o.name = req.name.strip()
    if req.contact_name is not None:
        o.contact_name = req.contact_name
    if req.contact_phone is not None:
        o.contact_phone = req.contact_phone
    if req.status is not None:
        o.status = req.status
    audit.log(db, user, "org_updated", target_type=o.org_type, target_id=o.id,
              reason="组织信息变更", detail=req.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True, "id": o.id, "name": o.name}


@router.get("/users")
def list_users(role: Optional[str] = Query(default=None),
               user: Dict[str, Any] = Depends(require_perm("admin:user")),
               db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = select(SysUser)
    if role:
        q = q.where(SysUser.role == role)
    rows = db.execute(q.order_by(SysUser.role, SysUser.id)).scalars().all()
    org_ids = {u.org_id for u in rows if u.org_id}
    orgs = {o.id: o.name for o in db.execute(
        select(SysOrg).where(SysOrg.id.in_(org_ids))).scalars().all()} if org_ids else {}
    return [{"id": u.id, "account": u.account, "name": u.display_name, "role": u.role,
             "org_id": u.org_id, "org_name": orgs.get(u.org_id), "status": u.status,
             "phone": u.phone,
             "last_login_at": u.last_login_at.isoformat(timespec="seconds")
             if u.last_login_at else None} for u in rows]


class UserReq(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=64)
    status: Optional[int] = Field(default=None, ge=0, le=1)
    password: Optional[str] = Field(default=None, min_length=6, max_length=64)


@router.put("/users/{uid}")
def update_user(uid: int, req: UserReq,
                user: Dict[str, Any] = Depends(require_perm("admin:user")),
                db: Session = Depends(get_db)) -> Dict[str, Any]:
    u = db.get(SysUser, uid)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    if req.display_name:
        u.display_name = req.display_name.strip()
    if req.status is not None:
        if uid == user["user_id"] and req.status == 0:
            raise HTTPException(status_code=409, detail="不能停用当前登录账号")
        u.status = req.status
    changed = req.model_dump(exclude_none=True, exclude={"password"})
    if req.password:
        u.password_hash = hash_password(req.password)
        changed["password"] = "重置"
    audit.log(db, user, "user_updated", target_type="user", target_id=u.id,
              reason="账号信息变更", detail=changed)
    db.commit()
    return {"ok": True, "id": u.id, "account": u.account, "status": u.status}


@router.put("/carriers/{org_id}")
def update_carrier_profile(org_id: int,
                           cost_per_km: Optional[float] = Query(default=None, gt=0, le=100),
                           capacity_t: Optional[float] = Query(default=None, gt=0, le=200),
                           user: Dict[str, Any] = Depends(require_perm("admin:org_manage")),
                           db: Session = Depends(get_db)) -> Dict[str, Any]:
    """超管维护承运商的每公里成本与可载吨位（这两项直接参与收益试算）。"""
    p = db.execute(select(CarrierProfile).where(
        CarrierProfile.org_id == org_id)).scalars().first()
    if not p:
        raise HTTPException(status_code=404, detail="该组织没有承运商档案")
    old = {"cost_per_km": float(p.cost_per_km), "capacity_t": float(p.capacity_t)}
    if cost_per_km is not None:
        p.cost_per_km = Decimal(str(cost_per_km))
    if capacity_t is not None:
        p.capacity_t = Decimal(str(capacity_t))
    audit.log(db, user, "carrier_profile_updated", target_type="carrier", target_id=org_id,
              reason="承运商档案变更",
              detail={"old": old, "new": {"cost_per_km": cost_per_km,
                                          "capacity_t": capacity_t}})
    db.commit()
    return {"ok": True, "cost_per_km": float(p.cost_per_km),
            "capacity_t": float(p.capacity_t)}


def _session():
    from ..db import session_as

    return session_as("app")
