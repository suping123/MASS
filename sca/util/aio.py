"""事件循环工具（S2 并发修复的地基）。

**要解决的问题**：本项目多处网络调用用标准库 `urllib.request.urlopen`——它是**同步阻塞**
的，却写在 `async def` 里。协程从不挂起，于是

  * `asyncio.gather` 的多分支被顺序跑完：P1 规划段扇出实测 **0/4 轮并行**（两个分支首尾
    相接，时间重叠 0ms）。也就是说拓扑上的"并行"在运行时是假的。
  * 更严重的是：整次调度期间 uvicorn 的事件循环被卡死，**期间所有 `/api/*` 请求都无响应**
    （单次路线规划阻塞 800–1000ms，模型调用可阻塞数秒）。这是既有缺陷，不是改造引入的。

**修法**：把阻塞部分放进线程池（`asyncio.to_thread`），事件循环在等待期间可以继续调度
其他协程。注意 `await asyncio.sleep(0)` **无效**——阻塞的 socket 调用不会因为一次让步就
变得可并发；必须把调用本身搬出事件循环。

**边界**：线程池默认容量 `min(32, cpu+4)`，对"一张图内最多几条并行分支"的规模足够；
不另建线程池，避免和 asyncio 的默认执行器产生两套线程。真正的高并发场景应换成
`httpx.AsyncClient`（连接池 + 原生异步），那是后续独立的一项，不在本次范围内。
"""
from __future__ import annotations

import asyncio
import urllib.request
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在线程池里执行同步阻塞函数，返回值原样透出（异常也原样抛出）。

    用于"同步函数被 async 调用"的场景，例如
    `parse_dispatch_request`（内部可能发模型 HTTP 请求）被 async 路由处理器直接调用。
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def fetch_bytes(req: Any, timeout: float = 8.0) -> bytes:
    """执行一次同步 urllib 请求并返回响应体字节。

    `req` 与 `urllib.request.urlopen` 一致：URL 字符串或 `urllib.request.Request`。
    调用方负责 `json.loads(...decode("utf-8"))` 等解析。

    保留「同步 urlopen + 异常原样抛出」的语义，只是把它挪到线程里——
    这样各调用点原有的 `except` / 凭证链降级逻辑一行都不用改。
    """
    def _do() -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    return await run_blocking(_do)
