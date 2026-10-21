"""在同步的 FastAPI 路由里调用异步的 MCP 工具。

为什么需要这层
--------------
MCP 工具（map-mcp / weather-mcp）都是 `async def`，而平台的路由与服务层全是同步
`def`（FastAPI 会丢到线程池，避免阻塞事件循环）。两条链路必须能接上。

为什么不用 asyncio.run 直接调
----------------------------
1. 如果调用点恰好在事件循环线程里（未来某个 async 路由），`asyncio.run` 会抛
   `RuntimeError: asyncio.run() cannot be called from a running event loop`；
2. 外部 API 可能挂住，必须带超时，不能让一次天气查询把请求线程拖死。

所以：没有运行中的 loop -> 直接 `asyncio.run`；有 -> 丢到独立线程里跑。
两条路径都带超时，超时与异常一律**向上抛**，由调用方决定降级方式——
这里不做静默兜底，否则"查不到天气"和"天气晴"就分不清了。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


class McpTimeout(RuntimeError):
    """MCP 调用超时。"""


def run_async(coro: Coroutine[Any, Any, Any], timeout: float = 25.0) -> Any:
    """同步等待一个 coroutine 的结果。超时抛 McpTimeout。"""
    async def _guard():
        return await asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        try:
            return asyncio.run(_guard())
        except asyncio.TimeoutError:
            raise McpTimeout(f"MCP 调用超时（{timeout}s）") from None

    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(_guard())
        except BaseException as exc:  # noqa: BLE001 要跨线程传回
            box["error"] = exc

    t = threading.Thread(target=_worker, name="mcp-bridge", daemon=True)
    t.start()
    t.join(timeout + 5)
    if t.is_alive():
        raise McpTimeout(f"MCP 调用超时（{timeout}s）")
    err = box.get("error")
    if isinstance(err, asyncio.TimeoutError):
        raise McpTimeout(f"MCP 调用超时（{timeout}s）") from None
    if err:
        raise err
    return box.get("value")


__all__ = ["run_async", "McpTimeout"]
