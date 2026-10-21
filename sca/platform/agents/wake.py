"""门铃：让常驻 agent 有消息就醒，而不是傻等轮询。

为什么要它
----------
一个 agent 循环里如果只有 ``sleep(poll_ms)``，想要"接单 5 秒内返回"就得把
poll_ms 压到 100ms 以内——四个进程每秒各查 10 次库，空转比干活还费。
门铃把等待变成阻塞的：``bus.send()`` 往收件人的门铃列表里塞一个令牌，
对方的 ``BLPOP`` 立刻返回，处理完再来等。

为什么用 Redis 列表而不是发布订阅
--------------------------------
``PUBLISH`` 在没有人订阅时消息直接丢掉，agent 重启的窗口里发的消息就永远叫不醒它。
列表是持久的：塞进去的令牌在进程回来之后还在，最多多醒一次（多醒是幂等的，
醒来发现没消息就继续等）。

**它不是投递通道**。真正的消息在 ``agent_message`` 表里，门铃丢了只会退化成
"晚一个轮询周期才处理"，不会丢消息。这条要说清楚，否则以后有人会以为
Redis 一挂整个链路就断了。
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, List, Optional

logger = logging.getLogger("sca.platform.agents.wake")

KEY_PREFIX = "sca:bus:wake:"
MAX_BELLS = 200          # 列表上限：一次塞太多没有意义，一次醒来会把积压的都领走
BELL_TTL_S = 600


def _enabled() -> bool:
    import os

    return (os.environ.get("SCA_AGENT_WAKE", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _client():
    from ..security import redis_client

    return redis_client()


def key_of(agent_key: str) -> str:
    return KEY_PREFIX + str(agent_key)


def ring(agent_key: str) -> bool:
    """按门铃。**永不抛异常**：叫不醒最多晚一个轮询周期，不该让发消息失败。"""
    if not _enabled() or not agent_key:
        return False
    try:
        k = key_of(agent_key)
        r = _client()
        pipe = r.pipeline()
        pipe.lpush(k, "1")
        pipe.ltrim(k, 0, MAX_BELLS - 1)
        pipe.expire(k, BELL_TTL_S)
        pipe.execute()
        return True
    except Exception as exc:                     # noqa: BLE001
        logger.debug("门铃不可用（%s），退化为轮询", exc)
        return False


def wait(keys: Iterable[str], timeout_s: float) -> Optional[str]:
    """等任意一个门铃响，最多 ``timeout_s`` 秒。超时返回 None。

    Redis 不可用时**不是立刻返回**（那会变成死循环空转），而是老老实实睡够
    超时时间——退化成纯轮询，行为仍然正确，只是慢。
    """
    keys: List[str] = [key_of(k) for k in keys if k]
    timeout = max(0.05, float(timeout_s))
    if not keys or not _enabled():
        time.sleep(timeout)
        return None
    try:
        r = _client()
        got = r.blpop(keys, timeout=timeout)
        if not got:
            return None
        name = got[0]
        if isinstance(name, bytes):
            name = name.decode("utf-8", "ignore")
        return str(name)[len(KEY_PREFIX):] if str(name).startswith(KEY_PREFIX) else None
    except Exception as exc:                     # noqa: BLE001
        logger.debug("门铃不可用（%s），本轮退化为 sleep", exc)
        time.sleep(timeout)
        return None


def drain(agent_key: str) -> int:
    """把门铃里的残留令牌清掉（启动时用，避免一起床就空转一圈）。"""
    try:
        r = _client()
        n = int(r.llen(key_of(agent_key)) or 0)
        if n:
            r.delete(key_of(agent_key))
        return n
    except Exception:                            # noqa: BLE001
        return 0


__all__ = ["key_of", "ring", "wait", "drain", "BELL_TTL_S", "MAX_BELLS"]
