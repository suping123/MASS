"""注册 / 登录 / 发码 / 当前用户。

三端共用这一组接口，角色决定登录后进入哪个页面：
  shipper    -> /shipper.html
  dispatcher -> /dispatch.html
  carrier    -> /carrier.html
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CarrierProfile, SysOrg, SysUser
from ..schemas import CodeReq, LoginReq, LoginResp, RegisterReq
from ..security import (create_token, current_user, hash_password, issue_captcha,
                        perms_of, verify_code, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_HOME = {"shipper": "/shipper.html", "dispatcher": "/dispatch.html",
         "carrier": "/carrier.html", "admin": "/dispatch.html"}

# 注册时 role -> 组织类型。调度员归平台方，不单独建企业。
_ORG_TYPE = {"shipper": "shipper", "carrier": "carrier", "dispatcher": "platform"}


@router.get("/captcha")
def captcha_image(purpose: str = "login", account: str = "",
                  db: Session = Depends(get_db)) -> Dict[str, Any]:
    """取一张图形验证码。

    purpose=register 时若账号已被占用则直接 409（提前拦，避免填完表单才报错）；
    purpose=login 时**不**校验账号是否存在——否则接口变成账号枚举工具。
    """
    if purpose not in ("login", "register"):
        raise HTTPException(status_code=400, detail="purpose 只能是 login 或 register")
    account = (account or "").strip().lower()
    if purpose == "register" and account:
        if db.execute(select(SysUser.id).where(SysUser.account == account)).first():
            raise HTTPException(status_code=409, detail="该账号已注册，请直接登录")
    return issue_captcha(purpose, account)


@router.post("/code")
def send_code(req: CodeReq, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """兼容旧入口：语义已从"发送验证码"改为"取一张图形验证码"。

    保留路径是为了不打断已有的脚本与书签；返回体与 /captcha 完全一致，
    同样不含任何明文答案。
    """
    account = (req.account or "").strip().lower()
    if req.purpose == "register" and account:
        if db.execute(select(SysUser.id).where(SysUser.account == account)).first():
            raise HTTPException(status_code=409, detail="该账号已注册，请直接登录")
    return issue_captcha(req.purpose, account)


def _get_or_create_org(db: Session, name: str, org_type: str) -> SysOrg:
    name = name.strip()
    org = db.execute(select(SysOrg).where(SysOrg.name == name)).scalar_one_or_none()
    if org:
        if org.org_type != org_type:
            raise HTTPException(
                status_code=409,
                detail=f"组织名「{name}」已被登记为{ {'shipper':'货主','carrier':'承运商','platform':'平台'}.get(org.org_type, org.org_type) }，不能重复用于注册",
            )
        return org
    org = SysOrg(name=name, org_type=org_type, contact_name=None, status=1)
    db.add(org)
    db.flush()
    return org


@router.post("/register")
def register(req: RegisterReq, db: Session = Depends(get_db)) -> Dict[str, Any]:
    account = req.account.strip().lower()
    if not account.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="账号只能包含字母、数字、下划线和短横线")
    if db.execute(select(SysUser.id).where(SysUser.account == account)).first():
        raise HTTPException(status_code=409, detail="该账号已注册，请直接登录")

    # 验证码最后校验：前面的业务校验失败时不白白消耗一次验证码
    verify_code("register", account, req.code, req.cid or "")

    org = _get_or_create_org(db, req.org_name, _ORG_TYPE[req.role])
    user = SysUser(
        account=account,
        password_hash=hash_password(req.password),
        display_name=req.display_name.strip(),
        role=req.role,
        org_id=org.id,
        phone=(req.phone or "").strip() or None,
        status=1,
    )
    db.add(user)
    db.flush()

    if req.role == "carrier":
        # 承运商默认画像：单价与吨位可后改；位置由承运商自行填写，不接车载 IoT
        if not db.execute(select(CarrierProfile.id).where(
                CarrierProfile.org_id == org.id)).first():
            db.add(CarrierProfile(org_id=org.id, carrier_user_id=user.id,
                                  cost_per_km=2.60, capacity_t=30.00))
    db.commit()

    token = create_token(user)
    return {"token": token, "user": _user_brief(user, perms_of(db, user.role)),
            "home": _HOME.get(user.role, "/")}


@router.post("/login", response_model=LoginResp)
def login(req: LoginReq, db: Session = Depends(get_db)):
    account = req.account.strip().lower()
    user = db.execute(select(SysUser).where(SysUser.account == account)).scalar_one_or_none()
    # 账号不存在与密码错误返回同一句话：不给账号枚举留口子
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if user.status != 1:
        raise HTTPException(status_code=403, detail="账号已停用，请联系平台管理员")

    verify_code("login", account, req.code, req.cid or "")

    user.last_login_at = datetime.now()
    db.commit()
    return LoginResp(token=create_token(user),
                     user=_user_brief(user, perms_of(db, user.role)),
                     home=_HOME.get(user.role, "/"))


@router.get("/me")
def me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"user": user, "home": _HOME.get(user["role"], "/")}


def _user_brief(user: SysUser, perms) -> Dict[str, Any]:
    return {"id": user.id, "account": user.account, "role": user.role,
            "display_name": user.display_name, "org_id": user.org_id,
            "perms": sorted(perms)}
