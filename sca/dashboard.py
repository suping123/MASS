"""看板数据提供（前后端分离后，后端只出 JSON，前端自行渲染）。

提供 dashboard_data() 供 API `GET /api/dashboard-data` 与离线快照脚本
（scripts/render_dashboard.py）使用。不再做服务端 HTML 渲染。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from sca.settings import settings


async def dashboard_data() -> Dict[str, Any]:
    """一次性返回前端看板所需数据：评估 + 健康状态。"""
    from sca.eval.harness import run_all

    rep = await run_all()
    return {
        "eval": {
            "metrics": [m.__dict__ for m in rep["metrics"]],
            "raw": {k: v for k, v in rep["raw"].items()
                    if not isinstance(v, dict)},
        },
        "health": {
            "status": "ok",
            "hitl_enabled": settings.hitl_enabled,
            "dispatch_review_enabled": settings.dispatch_review_enabled,
            "model": settings.intent_model,
            "model_available": bool(settings.dashscope_api_key),
        },
    }


def render() -> str:
    """已废弃：前后端分离后看板由前端渲染。保留签名以兼容旧调用。"""
    raise RuntimeError("render() 已废弃；看板改由前端(frontend/)渲染，"
                       "请访问 / 或运行 scripts/render_dashboard.py 生成离线快照")
