"""Windows 专用事件循环工厂：uvicorn 在 Windows 上默认返回 ProactorEventLoop，
而 psycopg(AsyncPostgresSaver) 无法在 Proactor 中建立异步连接，表现为
"couldn't get a connection after 15.00 sec"。这里强制返回 SelectorEventLoop 以连通 PG。

用法（启动命令加 --loop）：
    uvicorn app.main:app --loop sca.win_loop:win_selector_loop_factory
注意：uvicorn 对自定义 --loop 工厂会把本函数直接交给 asyncio.Runner，
Runner 以 loop_factory() 调用并期望返回「事件循环实例」（不是类），
与内置工厂返回类的契约不同——因此这里必须返回实例。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable


def win_selector_loop_factory(*args, **kwargs) -> asyncio.AbstractEventLoop:
    # uvicorn 对自定义 --loop 工厂：直接把本函数传给 asyncio.Runner，
    # Runner 会以 loop_factory() 调用并期望返回一个「事件循环实例」（不是类）。
    # 返回 SelectorEventLoop 实例，Windows 上即可让 psycopg 异步连接 PG。
    return asyncio.SelectorEventLoop()
