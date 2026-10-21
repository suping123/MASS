"""Web 层自测（零依赖，验证 handler 与前端目录，不启动服务）。"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import sca.api as api  # noqa: E402


async def main() -> None:
    print("health:", await api.h_health())
    # 用命中规则的短语（source=rule）做自测，避免触发真实模型与频率限制
    r = await api.h_route({"text": "冷链车温度超标"})
    print("route:", r)
    assert r.get("source") == "rule", "期望规则命中，未触发真实模型"
    # 命中模型的路径另测（不抛异常即可，429 限流时仍应优雅降级）
    r2 = await api.h_route({"text": "客户希望这批生鲜今天必须送到，怎么调度比较稳妥"})
    print("route(model fallback):", r2)
    ev = await api.h_eval()
    print("eval metrics:", len(ev["metrics"]), "| raw keys:", len(ev.get("raw") or {}))
    fe = api.resolve_frontend_dir()
    print("frontend dir:", fe, "exists:", os.path.isdir(fe))
    for f in ("index.html", "app.js", "style.css"):
        print("  ", f, os.path.isfile(os.path.join(fe, f)))


if __name__ == "__main__":
    asyncio.run(main())
