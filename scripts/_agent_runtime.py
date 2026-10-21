"""测试用：让四个 agent 在**进程内的线程**里跑起来。

为什么要有它
------------
S5 之后，"下单 / 挂牌 / 投标 / 接单"这些写操作都由 agent 进程消费消息后执行。
进程内测试（`TestClient` + `fakeredis`）里没有一个独立进程，所以这里的做法是
把与 `worker.py` 完全相同的 `AgentDaemon` 起在线程里——**跑的是同一份代码**，
只是把"操作系统进程"换成"线程"。这不是绕过总线：消息仍然进 `agent_message` 表，
由 daemon 领走、执行、回复，HTTP 那边照样在 `await_reply` 上等。

两个入口
--------
``start()`` / ``stop()``
    起线程，模拟"agent 常驻"。用于 HTTP 层测试——请求会真的阻塞等回复。
``drain()``
    不起线程，同步跑若干轮 ``tick()``。用于"我只想把积压的消息推完"的场景
    （类似以前的 `pump()`，但用的是真正的 daemon，不是另一套实现）。

门铃默认关掉
------------
线程 + fakeredis 的阻塞 `BLPOP` 容易互相等；`poll_ms` 压到 50ms 已经完全够快，
所以这里把 `SCA_AGENT_WAKE` 设为 0，退化成纯轮询。**门铃本身的行为**由
`test_agent_daemon.py` 单独测，不靠这里的副作用。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from sca.platform.agents import daemons, inbox


class Agents:
    """一组跑在线程里的常驻 agent。"""

    def __init__(self, kinds, poll_ms: int = 50, due_s: float = 0.5) -> None:
        self.kinds = list(kinds)
        self.poll_ms = poll_ms
        self.due_s = due_s
        self._daemons: List = []
        self._threads: List[threading.Thread] = []
        # stop() 之后线程引用要留着：`running` 需要判"真的退了没有"。
        # 早先 stop() 里把 _threads 清空，running 就成了恒 False 的假断言——
        # 线程明明还活着（卡在 due() 里），测试却报"已全部退出"，
        # 于是下一段"没有 agent 时必须失败"测的是"有 agent 时会怎样"。
        self._retired: List[threading.Thread] = []

    def start(self) -> "Agents":
        os.environ.setdefault("SCA_AGENT_WAKE", "0")
        self._retired = [t for t in self._retired if t.is_alive()]
        for kind in self.kinds:
            d = daemons.build(kind)
            d.poll_ms = self.poll_ms
            d.due_interval_s = self.due_s
            d.beat_interval_s = 2.0
            t = threading.Thread(target=d.run, name=f"agent-{kind}", daemon=True)
            t.start()
            self._daemons.append(d)
            self._threads.append(t)
        # 等到心跳写进 agent_actor：否则第一封消息可能落在"还没起来的 agent"上
        self.wait_ready()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        for d in self._daemons:
            d.stop()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.05, deadline - time.monotonic()))
        self._retired = list(self._threads)
        self._daemons.clear()
        self._threads.clear()

    def wait_stopped(self, timeout: float = 8.0) -> bool:
        """等线程真的退出。`stop()` 之后用它，而不是"我调过 stop 了"。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    def alive_keys(self) -> Dict[str, bool]:
        from sca.platform.agents import bus
        from sca.platform.db import session_as

        db = session_as()
        try:
            return {k: bus.alive(db, k, stale_s=30) for k in _keys_of(self.kinds)}
        except Exception:                       # noqa: BLE001 探测失败按"没起来"算
            return {}
        finally:
            db.close()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """等到每个 agent 都写过心跳。返回是否全部就绪。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self.alive_keys().values()):
                return True
            time.sleep(0.1)
        return False

    @property
    def running(self) -> bool:
        """还有没有活着的线程。`stop()` 之后必须为 False——包括已退役的引用。"""
        return any(t.is_alive() for t in list(self._threads) + list(self._retired))

    def __enter__(self) -> "Agents":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()


def _keys_of(kinds) -> List[str]:
    """这些 kind 的收件箱名（承运商按组织分箱，从库里取实际存在的）。"""
    from sca.platform.agents.daemons import carrier_daemon
    from sca.platform.db import session_as

    out: List[str] = []
    for k in kinds:
        if k == "carrier":
            db = session_as()
            try:
                out.extend(carrier_daemon.keys(db))
            finally:
                db.close()
        else:
            out.append(k)
    return out


def start(kinds=None, **kw) -> Agents:
    """起线程版常驻 agent。默认四个都起（成交链会用到 dispatcher 的 due）。"""
    return Agents(kinds or daemons.KINDS, **kw).start()


def drain(max_rounds: int = 8) -> Dict[str, int]:
    """同步把积压的消息推完（每轮四个 kind 各跑一次 once-tick）。"""
    os.environ.setdefault("SCA_AGENT_WAKE", "0")
    total = {"rounds": 0, "handled": 0, "failed": 0, "skipped": 0}
    for _ in range(max(1, int(max_rounds))):
        moved = 0
        for kind in daemons.KINDS:
            try:
                s = daemons.build(kind, once=True).tick()
            except KeyError:                    # 没有该 kind 的 daemon
                continue
            moved += s["handled"] + s["failed"] + s["skipped"]
            for k in ("handled", "failed", "skipped"):
                total[k] += s[k]
        total["rounds"] += 1
        if not moved:
            break
    return total


def clear_inbox(*keys: str) -> int:
    """清空收件箱里**还没处理**的消息。返回清掉几条。

    只删 pending/working：已经 ack 的消息是"协作记录"，删了之后
    `bus.thread()` 就回放不出"谁委托过什么"，测试会以为链路没走过。
    """
    from sqlalchemy import delete

    from sca.platform.db import session_as
    from sca.platform.models import AgentMessage

    if not keys:
        return 0
    db = session_as()
    try:
        n = int(db.execute(delete(AgentMessage).where(
            AgentMessage.to_agent.in_(list(keys)),
            AgentMessage.status.in_(("pending", "working")))).rowcount or 0)
        db.commit()
        return n
    finally:
        db.close()


def flush_human() -> int:
    """把"人"的收件箱清干净。

    人没有 daemon，回复本该由 `await_reply` 读取时 ack 掉；但如果 HTTP 已经超时
    才收到回复，那条会一直 pending。测试之间留着它会干扰"到底谁处理了消息"的判断。
    """
    from sqlalchemy import select

    from sca.platform.db import session_as
    from sca.platform.models import AgentMessage

    db = session_as()
    try:
        rows = db.execute(select(AgentMessage).where(
            AgentMessage.to_agent.like("human:%"),
            AgentMessage.status.in_(("pending", "working")))).scalars().all()
        for m in rows:
            m.status = "ack"
            m.error = "人类请求方已断开"
        db.commit()
        return len(rows)
    finally:
        db.close()


def pending(agent_key: str) -> int:
    from sca.platform.agents import bus
    from sca.platform.db import session_as

    db = session_as()
    try:
        return bus.pending_count(db, agent_key)
    finally:
        db.close()


def wait_until(fn, timeout: float = 8.0, step: float = 0.1) -> bool:
    """轮询等一个条件成立。**每次重新求值**，不要在闭包里缓存会话。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


__all__ = ["Agents", "start", "drain", "clear_inbox", "flush_human", "pending",
           "wait_until", "inbox"]
