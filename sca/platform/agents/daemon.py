"""常驻 Agent 的循环骨架。

一个 agent 进程的一生
--------------------
::

    while not stopped:
        1. 领自己的收件箱（带租约）
        2. 逐条处理，处理完 ack；失败退回复队列，超次数判死
        3. 到点了就跑一次 due()：到期裁决、协商超时、在途巡查
        4. 顺手写心跳（HTTP 侧靠它判断"agent 到底起没起"）
        5. 没有活就等门铃（最多 poll_ms）

第 3 步是"不等人叫"的来源。只做 1-2 步的话，agent 仍然是被动的：开标之后没人
催就不会裁决，协商超时也不会自己判死。所以 due() 不是可选项。

为什么每个进程都要自己的循环
----------------------------
把四个角色放进一个循环里、依次处理各自的收件箱，也能跑通同样的业务——但那不叫
多 agent，那叫一个 agent 有四个名字：调度卡住时承运商跟着一起停，调度崩溃时
承运商的活也没人干。S4 的验收就是照着这一点打的：杀掉 dispatch，carrier 的日志
必须继续增长。

失败与重试
----------
handler 抛异常 → 回滚整条消息的全部写入（包括它已经发出的消息）→ 退回复队列。
所以 handler 里**不许自己 commit**：一次半成品写入比失败更糟，它会让重试看到
一个自相矛盾的状态。重试超过 `inbox.MAX_TRIES` 判 failed 并留错误原因。
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..db import session_as
from ..models import AgentMessage
from . import inbox, wake

logger = logging.getLogger("sca.platform.agents.daemon")

Handler = Callable[[Session, AgentMessage], Optional[Dict[str, Any]]]

HOST = socket.gethostname()[:64]


class AgentDaemon:
    """子类只需要给出三样东西：自己的收件箱、每种消息怎么处理、到点了做什么。"""

    kind: str = ""                 # dispatch / carrier / exception / shipper
    poll_ms: int = 500             # 没活时最多等多久（也是门铃的可接受延迟上限）
    lease_s: int = 60              # 租约时长：一次处理不该超过它
    batch: int = 10                # 一轮最多领几条
    due_interval_s: float = 20.0   # 到期检查间隔
    beat_interval_s: float = 10.0  # 心跳间隔

    # ---- 子类实现 -------------------------------------------------------
    def keys(self, db: Session) -> List[str]:
        """自己负责哪些收件箱。承运商是 ``carrier:{org}``，其余是单例。"""
        raise NotImplementedError

    def handlers(self) -> Dict[str, Handler]:
        """msg_type -> 处理函数。没列出的类型会被 ack 而非无限重投。"""
        raise NotImplementedError

    def due(self, db: Session, keys: List[str]) -> Dict[str, Any]:
        """定时任务。默认什么都不做。"""
        return {}

    def on_start(self, db: Session) -> None:
        """启动时的准备（注册身份等）。"""

    # ---- 机制 -----------------------------------------------------------
    def __init__(self, *, once: bool = False,
                 only_keys: Optional[List[str]] = None) -> None:
        self.once = bool(once)
        # 限定只服务这些收件箱（`--org 9912` → carrier:9912）。
        # 一个进程只盯一个 org 时，别的 org 的消息不会被动到——排查/演示都需要
        # "这个进程到底动了谁的消息"是确定的。
        self.only_keys = list(only_keys or [])
        self._stop = False
        self._keys: List[str] = []
        self._last_due = 0.0
        self._last_beat = 0.0
        self._last_idle_log = 0.0
        self._started_at = time.monotonic()

    def _filter(self, keys: List[str]) -> List[str]:
        if not self.only_keys:
            return keys
        return [k for k in keys if k in self.only_keys]

    # ------------------------------------------------------------------
    def stop(self, *_args) -> None:
        self._stop = True
        logger.info("[%s] 收到停止信号，本批处理完就退出", self.kind)

    def _install_signals(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):     # 非主线程 / Windows 某些场景
                pass

    # ------------------------------------------------------------------
    def tick(self) -> Dict[str, Any]:
        """跑一轮：处理消息 + 到点检查 + 心跳。返回统计。"""
        stats: Dict[str, Any] = {"handled": 0, "failed": 0, "skipped": 0,
                                 "results": [], "due": {}}
        db = session_as()
        try:
            keys = self._filter(self.keys(db))
            self._keys = keys
            handlers = self.handlers()

            for key in keys:
                for m in inbox.claim(db, key, limit=self.batch,
                                     lease_s=self.lease_s):
                    try:
                        fn = handlers.get(m.msg_type)
                        if fn is None:
                            # 没处理器的消息不能留 pending：下一轮还会领到它，
                            # 一条没人认领的消息会把收件箱堵住。
                            inbox.finish(db, m, ok=True,
                                         error=f"无 {m.msg_type} 处理器，已忽略")
                            db.commit()
                            stats["skipped"] += 1
                            continue
                        r = fn(db, m)
                        db.commit()
                        if m.status == "working":       # handler 没自己 ack
                            inbox.finish(db, m, ok=True,
                                         error="" if r is not None else "stale")
                            db.commit()
                        if r is None:
                            stats["skipped"] += 1
                        else:
                            stats["handled"] += 1
                            stats["results"].append({"msg_id": m.id, **r})
                    except Exception as exc:            # noqa: BLE001
                        db.rollback()
                        mid = m.id
                        logger.exception("[%s] 处理消息 #%s 失败", self.kind, mid)
                        fresh = db.get(AgentMessage, mid)
                        if fresh is not None:
                            inbox.finish(
                                db, fresh, ok=False,
                                error=f"{type(exc).__name__}: {exc}"[:200])
                        db.commit()
                        stats["failed"] += 1

            now = time.monotonic()
            if (self.once or not self._last_due
                    or now - self._last_due >= self.due_interval_s):
                self._last_due = now
                try:
                    stats["due"] = self.due(db, keys)
                    db.commit()
                except Exception as exc:                # noqa: BLE001
                    db.rollback()
                    logger.exception("[%s] due() 失败", self.kind)
                    stats["due"] = {"error": f"{type(exc).__name__}: {exc}"}

            if self.once or now - self._last_beat >= self.beat_interval_s:
                self._last_beat = now
                self._beat(db, keys)
            return stats
        finally:
            db.close()

    def _beat(self, db: Session, keys: List[str]) -> None:
        from . import bus

        for k in keys:
            try:
                bus.touch(db, k, pid=os.getpid(), host=HOST, commit=False)
            except Exception:                           # noqa: BLE001
                logger.debug("[%s] 心跳写入失败（不影响业务）", self.kind, exc_info=True)
        try:
            db.commit()
        except Exception:                               # noqa: BLE001
            db.rollback()

    # ------------------------------------------------------------------
    def run(self) -> int:
        self._install_signals()
        logger.info("[%s] 启动 poll=%dms lease=%ds due=%.0fs pid=%d",
                    self.kind, self.poll_ms, self.lease_s, self.due_interval_s,
                    os.getpid())
        db = session_as()
        try:
            self.on_start(db)
            db.commit()
            keys = self._filter(self.keys(db))
            self._keys = keys
        except Exception:
            logger.exception("[%s] 启动失败", self.kind)
            return 2
        finally:
            db.close()

        for k in self._keys:
            wake.drain(k)               # 清掉历史令牌，避免一启动就空转一圈

        idle_log = 0.0
        while not self._stop:
            try:
                s = self.tick()
            except Exception:           # noqa: BLE001 循环本身不能死
                logger.exception("[%s] tick 异常，1 秒后重试", self.kind)
                time.sleep(1.0)
                continue

            worked = s["handled"] + s["failed"] + s["skipped"]
            # due() 干了活也算"这一轮有动作"。只看收件箱会漏掉最要紧的那种行为：
            # 到期裁决、协商超时判死全在 due() 里，它们不留日志的话，
            # "agent 自己在推进"就只剩页面上的状态变化，排查时看不到是谁做的。
            if worked or s["due"]:
                logger.info("[%s] 本轮 handled=%d failed=%d skipped=%d due=%s",
                            self.kind, s["handled"], s["failed"], s["skipped"],
                            s["due"] or "-")
                idle_log = 0.0
            elif time.monotonic() - idle_log >= max(10.0, self.due_interval_s):
                # 什么都没干也要周期性留一行：运维用它判断"进程活着但没活"，
                # 跟"进程死了"是两种完全不同的故障。
                idle_log = time.monotonic()
                logger.info("[%s] 空闲 uptime=%ds pending=%s",
                            self.kind, int(time.monotonic() - self._started_at),
                            self._depth())

            if self.once:
                break
            if not worked:
                wake.wait(self._keys, self.poll_ms / 1000.0)

        logger.info("[%s] 退出", self.kind)
        return 0

    def _depth(self) -> Dict[str, int]:
        db = session_as()
        try:
            return {k: inbox.depth(db, k) for k in self._keys}
        except Exception:                               # noqa: BLE001
            return {}
        finally:
            db.close()


__all__ = ["AgentDaemon", "Handler"]
