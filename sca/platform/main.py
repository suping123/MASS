"""三平台调度系统的 HTTP 入口（与旧的单页系统并行，切换时只改挂载）。

启动：
    ./.venv/Scripts/python.exe -m uvicorn sca.platform.main:app --port 8100 ^
        --loop sca.win_loop:win_selector_loop_factory

为什么仍带 `--loop`：调度内核复用现有图，内部用 psycopg 异步连接，
Windows 默认 Proactor 循环建不上连（见项目既有约束）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .db import ping as db_ping
from .db import session_as
from .routers import (admin, auth, carrier, dispatch, knowledge,
                        notification, shipper)
from .security import redis_client

logger = logging.getLogger("sca.platform")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend_v2"


def create_app() -> FastAPI:
    app = FastAPI(title="供应链三平台调度系统", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # 三端同源部署；分离部署时按域名收紧
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(shipper.router)
    app.include_router(dispatch.router)
    app.include_router(knowledge.router)
    app.include_router(admin.router)
    app.include_router(carrier.router)
    app.include_router(notification.router)

    # 静态资源缓存口径。
    #
    # 为什么必须显式写死：StaticFiles 默认只给 ETag / Last-Modified，不给 Cache-Control，
    # 浏览器于是按启发式规则自行缓存（通常是「距上次修改时间」的 10%）。旧单页系统
    # 曾经也在同一个 /common.js 路径上进过浏览器缓存，结果新页面拿到了不含 statusTag
    # 的旧脚本，订单列表整块渲染失败（报 "statusTag is not defined"）。
    # 口径：HTML 一律不缓存（改一版立刻生效），CSS / JS 每次回源校验（304 很便宜）。
    @app.middleware("http")
    async def _static_cache(request: Request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if path.endswith(".html") or path == "/":
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        elif path.endswith((".css", ".js")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/health")
    def health() -> JSONResponse:
        db = db_ping()
        try:
            redis_client().ping()
            rd = {"ok": True}
        except Exception as exc:  # noqa: BLE001 健康检查要暴露失败原因
            rd = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(db.get("ok")) and bool(rd.get("ok"))
        return JSONResponse(status_code=200 if ok else 503,
                            content={"ok": ok, "mysql": db, "redis": rd,
                                     "php": "n/a"})

    # 根路径直接进登录页。
    # 必须在 mount("/") 之前注册：Starlette 按注册顺序匹配，先注册的 Route 优先于 Mount。
    # 否则访问 http://127.0.0.1:8000 会因目录里没有 index.html 而返回 404 ——
    # 用户第一下就打不开，等于切换失败。
    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/login.html", status_code=307)

    if FRONTEND.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="web")

    @app.on_event("startup")
    def _startup() -> None:
        info = db_ping()
        if info.get("ok"):
            logger.info("MySQL 已连接：%s %s", info.get("database"), info.get("version"))
        else:
            logger.error("MySQL 连接失败：%s", info.get("error"))
        try:
            redis_client().ping()
            logger.info("Redis 已连接：%s", config.REDIS_URL)
        except Exception as exc:  # noqa: BLE001 启动不阻塞，但必须明确告知
            logger.error("Redis 不可用（验证码登录将返回 503）：%s: %s",
                         type(exc).__name__, exc)

        # 把库里的「模型降级」配置同步进进程开关。
        # 不同步的话，管理页面改完只有重启才生效（内核读的是 runtime_flags）。
        try:
            from sca import runtime_flags

            from .services import runtime

            db = session_as("app")
            try:
                conf = runtime.get(db, "model.enabled") or {}
            finally:
                db.close()
            runtime_flags.apply_from_config(conf if isinstance(conf, dict) else {},
                                            source="startup")
            logger.info("模型开关：%s", runtime_flags.snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型开关同步失败（沿用默认：允许调用）：%s", exc)

    return app


app = create_app()
