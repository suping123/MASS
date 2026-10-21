"""数据库连接。

表结构由 `sql/schema.sql` 建立，本模块不执行 DDL（避免 ORM 与手工 SQL 两份定义漂移）。

为什么要支持多账号：RBAC 只挡得住 HTTP，挡不住 SQL。若三个 agent 共用一个
数据库账号，货主侧的 agent 在代码层就能 SELECT 到承运商的 cost_per_km 与底价。
因此对外暴露 `session_as("shipper")` 之类的入口，让越权在数据库层就物理不可能。
"""
from __future__ import annotations

from typing import Dict, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config

_engines: Dict[str, Engine] = {}
_factories: Dict[str, sessionmaker] = {}

# agent 身份 -> 数据库账号（未单独配置时回落到主账号）
_AGENT_ACCOUNTS = {
    "shipper": ("SCA_DB_USER_SHIPPER", "SCA_DB_PASSWORD_SHIPPER"),
    "dispatcher": ("SCA_DB_USER_DISPATCH", "SCA_DB_PASSWORD_DISPATCH"),
    "carrier": ("SCA_DB_USER_CARRIER", "SCA_DB_PASSWORD_CARRIER"),
}


def get_engine(as_role: str = "app") -> Engine:
    if as_role in _engines:
        return _engines[as_role]
    import os

    user = password = ""
    if as_role in _AGENT_ACCOUNTS:
        ku, kp = _AGENT_ACCOUNTS[as_role]
        user = os.environ.get(ku, "")
        password = os.environ.get(kp, "")
    url = config.db_url(user, password)
    eng = create_engine(
        url,
        pool_size=config.DB_POOL_SIZE,
        max_overflow=4,
        pool_pre_ping=True,          # 断线自动重连（MySQL wait_timeout 会断）
        pool_recycle=config.DB_POOL_RECYCLE,
        future=True,
    )
    _engines[as_role] = eng
    _factories[as_role] = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    return eng


def session_as(as_role: str = "app") -> Session:
    """同步 Session。路由用 `def`（FastAPI 自动放到线程池），避免阻塞事件循环。"""
    if as_role not in _factories:
        get_engine(as_role)
    return _factories[as_role]()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：调度平台/管理端使用主账号。"""
    db = session_as("app")
    try:
        yield db
    finally:
        db.close()


def ping() -> Dict[str, object]:
    """健康检查：返回库名与版本，便于启动时确认连的是哪个 MySQL。"""
    try:
        with get_engine("app").connect() as conn:
            row = conn.execute(text("SELECT DATABASE(), VERSION()")).one()
        return {"ok": True, "database": row[0], "version": row[1]}
    except Exception as exc:  # noqa: BLE001 健康检查要报出原因，不能吞
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
