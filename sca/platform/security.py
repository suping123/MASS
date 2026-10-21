"""认证与鉴权：密码哈希 / JWT / 图形验证码 / 权限依赖。

三条硬约定：
  1) 验证码走 Redis（图形，答案只在服务端），Redis 不可用时 **fail-closed**（抛 503），
     绝不降级成"免验证码"；
  2) 权限点从数据库读，JWT 里只放 user_id / role，权限变更无需重新登录；
  3) 任何凭据都不进日志，jwt_secret 未配置时自动生成并落盘到 data/.jwt_secret。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import captcha, config
from .db import get_db
from .models import SysPermission, SysRole, SysRolePermission, SysUser

# ------------------------------------------------------------------ 密码
_PBKDF2_ROUNDS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, expect = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(dk.hex(), expect)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ JWT
_secret_cache: Optional[str] = None


def _jwt_secret() -> str:
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    if config.JWT_SECRET:
        _secret_cache = config.JWT_SECRET
        return _secret_cache
    path = config.ROOT / "data" / ".jwt_secret"
    if path.exists():
        _secret_cache = path.read_text(encoding="utf-8").strip()
        return _secret_cache
    path.parent.mkdir(parents=True, exist_ok=True)
    _secret_cache = secrets.token_urlsafe(48)
    path.write_text(_secret_cache, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return _secret_cache


def create_token(user: SysUser) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "account": user.account,
        "role": user.role,
        "org_id": user.org_id,
        "name": user.display_name,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=config.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=config.JWT_ALG)


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[config.JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="登录凭证无效") from None


# ------------------------------------------------------------------ Redis
_redis = None


def redis_client():
    """同步客户端。Redis 未启动时这里会抛错，由调用方转成 503。"""
    global _redis
    if _redis is None:
        import redis as _r

        _redis = _r.Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_timeout=3,          # 必须设：Redis 挂掉时不能把请求线程卡死
            socket_connect_timeout=3,
        )
    return _redis


def _redis_or_503():
    try:
        r = redis_client()
        r.ping()
        return r
    except Exception as exc:  # noqa: BLE001 要报出根因，别让用户猜
        raise HTTPException(
            status_code=503,
            detail=f"验证码服务不可用（Redis 未启动或连接失败）：{type(exc).__name__}",
        ) from None


_CODE_TRY = "sca:code:try:{purpose}:{account}"
_CODE_LOCK = "sca:code:lock:{purpose}:{account}"
_CAP_KEY = "sca:cap:{cid}"                      # 图形验证码答案，一次性


def issue_captcha(purpose: str, account: str = "") -> Dict[str, Any]:
    """生成一张图形验证码。

    答案只存服务端 Redis（键按 cid 随机，不按账号），响应里只有图片本身——
    任何形式的明文回显都会让"人机校验"退化成一次多余的网络往返。
    图片与账号解耦：账号填错时不必重看图，只有答错/过期才需要换。
    """
    r = _redis_or_503()
    if account:
        acc = account.strip().lower()
        if r.exists(_CODE_LOCK.format(purpose=purpose, account=acc)):
            raise HTTPException(status_code=429, detail="尝试次数过多，请 10 分钟后再试")

    code = captcha.new_code()
    cid = secrets.token_urlsafe(16)
    r.setex(_CAP_KEY.format(cid=cid), config.CODE_TTL, code)
    return {"ok": True, "cid": cid, "image": captcha.data_uri(code),
            "length": captcha.CODE_LEN, "expires_in": config.CODE_TTL}


def verify_code(purpose: str, account: str, code: str, cid: str = "") -> None:
    """校验图形验证码。答错即废掉这张图——否则一张图可以拿来穷举 4 位字符。"""
    r = _redis_or_503()
    account = account.strip().lower()
    if r.exists(_CODE_LOCK.format(purpose=purpose, account=account)):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 10 分钟后再试")

    if not (cid or "").strip():
        raise HTTPException(status_code=400, detail="验证码已失效，请点击图片换一张")
    key = _CAP_KEY.format(cid=cid.strip())
    expect = r.get(key)
    if not expect:
        raise HTTPException(status_code=400, detail="验证码已过期，请点击图片换一张")

    if not hmac.compare_digest(str(expect), captcha.normalize(code)):
        r.delete(key)
        tkey = _CODE_TRY.format(purpose=purpose, account=account)
        n = r.incr(tkey)
        r.expire(tkey, config.CODE_TTL)
        if n >= config.CODE_MAX_TRY:
            r.setex(_CODE_LOCK.format(purpose=purpose, account=account), 600, "1")
            raise HTTPException(status_code=429, detail="验证码错误次数过多，已锁定 10 分钟")
        raise HTTPException(status_code=400,
                            detail=f"验证码不正确，还可尝试 {config.CODE_MAX_TRY - n} 次（已为你换一张）")
    r.delete(key)
    r.delete(_CODE_TRY.format(purpose=purpose, account=account))


# ------------------------------------------------------------------ 依赖
def perms_of(db: Session, role_code: str) -> Set[str]:
    rows = db.execute(
        select(SysPermission.code)
        .join(SysRolePermission, SysRolePermission.perm_id == SysPermission.id)
        .join(SysRole, SysRole.id == SysRolePermission.role_id)
        .where(SysRole.code == role_code)
    ).all()
    return {r[0] for r in rows}


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    return auth[7:].strip()


def current_user(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    claims = decode_token(_bearer(request))
    try:
        uid = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="登录凭证无效") from None
    user = db.get(SysUser, uid)
    if not user or user.status != 1:
        raise HTTPException(status_code=403, detail="账号不存在或已停用")
    return {
        "user_id": user.id,
        "account": user.account,
        "role": user.role,
        "org_id": user.org_id,
        "display_name": user.display_name,
        "perms": perms_of(db, user.role),
    }


def require_perm(code: str):
    """依赖工厂：`user = Depends(require_perm("order:create"))`。"""

    def _dep(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if code not in user["perms"]:
            raise HTTPException(status_code=403, detail=f"无权限：{code}")
        return user

    return _dep


def require_role(*roles: str):
    def _dep(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="该账号不能访问此平台")
        return user

    return _dep


# ------------------------------------------------------------------ 工具
def now_ms() -> int:
    return int(time.time() * 1000)
