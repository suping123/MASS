"""Agent 消息总线 + 私有记忆。

这是多 Agent 架构的**运行时**部分。两条硬规则：

1. Agent 之间只经 `send()` 通信，不直接调用对方实现，也不读对方记忆。
2. 私有记忆按 `agent_key` 隔离——`recall("carrier:12", ...)` 读不到
   `carrier:13` 或 `dispatch` 的任何东西。

为什么不用消息队列
------------------
平台是单进程 uvicorn（`.bat` 一键启动），引入 Redis Stream / Celery 会破坏现有
启动链，运维成本也不划算。这里用"表 + 轮询"：消息落库后由收件方领取。

S4 起收件方是四个常驻进程（`agents/worker.py`），不再是"人类操作之后推一轮"。
表仍然是唯一真相，Redis 只做门铃（`wake.py`）——叫不醒最多晚一个轮询周期。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError, ProgrammingError
from sqlalchemy.orm import Session

from ..models import AgentActor, AgentMemory, AgentMessage
from . import contract

logger = logging.getLogger("sca.platform.agents.bus")

# 心跳多久没更新就算"进程不在了"。worker 每 2 秒跳一次，90 秒给了
# 45 个周期的余量：宁可短暂误报在线，也不要因为一次 GC 卡顿就显示离线——
# 页面显示离线会让人去重启进程，而重启一个正在干活的 agent 代价更大。
ALIVE_STALE_S = 90.0


class BusUnavailable(RuntimeError):
    """消息表不存在——通常是没有执行 sql/platform_v5.sql。"""


class NoReply(RuntimeError):
    """等不到 Agent 的回复：要么它没在跑，要么它忙得超过了等待上限。

    **不是业务失败**。业务上的"不行"是有回复的（``reject`` + 理由），
    这里代表"没人接这个活"。两者的处置完全不同：前者改条件重试，
    后者去 ``start_agents.bat`` 看进程。所以不能合成一个异常。
    """

    def __init__(self, msg_id: int, to_agent: str, waited_s: float) -> None:
        super().__init__(f"{to_agent} 在 {waited_s:.1f}s 内没有回复（消息 #{msg_id}）")
        self.msg_id = msg_id
        self.to_agent = to_agent
        self.waited_s = waited_s


def wait_timeout(default: float | None = None) -> float:
    """等待上限（秒）。``SCA_AGENT_WAIT_S`` 可调：测试压到 0.5，生产留 5。

    5 秒不是拍脑袋：真实 HTTP 链路上"点了接单到看到结果"超过 3 秒人就会重复点，
    而 agent 正常处理一条消息在 100ms 量级，5 秒已经是很宽的余量。
    """
    if default is not None:
        return float(default)
    try:
        return float(os.environ.get("SCA_AGENT_WAIT_S", "5"))
    except (TypeError, ValueError):
        return 5.0


def _guard(exc: Exception) -> None:
    if isinstance(exc, (ProgrammingError, DatabaseError)) and "agent_" in str(exc):
        raise BusUnavailable(
            "Agent 消息表不存在，请先执行 sql/platform_v5.sql") from exc
    raise exc


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------
def register(db: Session, agent_key: str, kind: str,
             user_id: Optional[int] = None, org_id: Optional[int] = None,
             utility: Optional[Dict[str, float]] = None) -> AgentActor:
    """注册（或懒创建）Agent 身份。幂等：已存在则补齐缺失字段。"""
    try:
        a = db.execute(select(AgentActor).where(
            AgentActor.agent_key == agent_key)).scalars().first()
        if a:
            if utility and not a.utility:
                a.utility = dict(utility)
            if user_id and not a.user_id:
                a.user_id = user_id
            if org_id and not a.org_id:
                a.org_id = org_id
            return a
        a = AgentActor(agent_key=agent_key, kind=kind, user_id=user_id,
                       org_id=org_id, utility=utility, enabled=1)
        db.add(a)
        db.flush()
        return a
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        raise


def utility_of(db: Session, agent_key: str) -> Dict[str, float]:
    """目标权重。缺省值是"各打五十大板"，但真实部署应各自不同，
    否则多 Agent 就失去意义（目标一致的 Agent 应该合并）。"""
    try:
        a = db.execute(select(AgentActor).where(
            AgentActor.agent_key == agent_key)).scalars().first()
        return dict(a.utility or {}) if a else {}
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return {}


# ---------------------------------------------------------------------------
# 私有记忆
# ---------------------------------------------------------------------------
def remember(db: Session, agent_key: str, mkey: str, value: Any) -> None:
    """写自己的记忆。key 前缀不做限制，隔离靠 agent_key。"""
    try:
        row = db.execute(select(AgentMemory).where(
            AgentMemory.agent_key == agent_key,
            AgentMemory.mkey == mkey)).scalars().first()
        if row:
            row.mval = value
        else:
            db.add(AgentMemory(agent_key=agent_key, mkey=mkey, mval=value))
        db.flush()
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)


def recall(db: Session, agent_key: str, mkey: str, default: Any = None) -> Any:
    """读自己的记忆。**签名里没有"读谁的"参数**——读别人根本不在接口上。"""
    try:
        row = db.execute(select(AgentMemory).where(
            AgentMemory.agent_key == agent_key,
            AgentMemory.mkey == mkey)).scalars().first()
        return row.mval if row else default
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return default


def memories(db: Session, agent_key: str, limit: int = 50) -> List[Dict[str, Any]]:
    try:
        rows = db.execute(select(AgentMemory).where(
            AgentMemory.agent_key == agent_key).order_by(
            AgentMemory.updated_at.desc()).limit(limit)).scalars().all()
        return [{"key": r.mkey, "value": r.mval,
                 "updated_at": r.updated_at.isoformat(timespec="seconds")
                 if r.updated_at else None} for r in rows]
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return []


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
def send(db: Session, *, from_agent: str, to_agent: str, msg_type: str,
         payload: Optional[Dict[str, Any]] = None,
         thread_id: Optional[str] = None, order_id: Optional[int] = None,
         exception_id: Optional[int] = None,
         parent_id: Optional[int] = None,
         commit: bool = False) -> AgentMessage:
    """发一条消息，先过契约校验再落库，然后按一下对方的门铃。

    契约校验放在这里而不是各 Agent 自己写：漏一处就等于没约束。

    门铃在 flush 之后按，不在 commit 之后：调用方常用 ``commit=False`` 把
    "发消息"和"改业务状态"放进同一个事务（协商就是这样，不能出现
    "消息发了、招标状态没改"）。这时门铃可能比 commit 早几毫秒响一次，
    对方醒来没看到消息就继续等——**正确性由轮询周期兜底，门铃只是加速**。
    """
    payload = contract.validate(from_agent, to_agent, msg_type, payload)
    tid = thread_id or contract.thread_of(order_id=order_id,
                                          exception_id=exception_id)
    try:
        m = AgentMessage(thread_id=tid, parent_id=parent_id,
                         from_agent=from_agent, to_agent=to_agent,
                         msg_type=msg_type, payload=payload, status="pending")
        db.add(m)
        db.flush()
        if commit:
            db.commit()
        from . import wake as _wake          # 局部 import：wake 可能用到 security

        _wake.ring(to_agent)
        return m
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        raise


def reply(db: Session, msg: AgentMessage, msg_type: str,
          payload: Optional[Dict[str, Any]] = None,
          *, commit: bool = False) -> AgentMessage:
    """回一条消息给发件人，自动带上 ``parent_id`` 与同一 thread。

    收发双方写错一个字段，消息树就断了——回放时看到"有人说了句话，
    但不知道在回谁"。所以「给谁回、挂在谁下面」由总线自己算，不由调用方填。
    """
    return send(db, from_agent=msg.to_agent, to_agent=msg.from_agent,
                msg_type=msg_type, payload=payload,
                thread_id=msg.thread_id, parent_id=msg.id, commit=commit)


def claim(db: Session, agent_key: str, limit: int = 20) -> List[AgentMessage]:
    """取走发给自己的待处理消息（按 id 升序，保证同一 thread 内顺序）。

    v5 的同步领取：取完即 ``consumed``。S4 之后生产路径改用
    ``inbox.claim``（带租约），这个留着给测试与"手动推一轮"。

    加了 ``SKIP LOCKED``：常驻进程可能同时在跑，两边同时领到同一条消息
    会导致报价翻倍。跳过被锁住的行是最小的代价。
    """
    try:
        rows = db.execute(select(AgentMessage).where(
            AgentMessage.to_agent == agent_key,
            AgentMessage.status == "pending").order_by(
            AgentMessage.id.asc()).limit(limit)
            .with_for_update(skip_locked=True)).scalars().all()
        for m in rows:
            m.status = "consumed"
            m.consumed_at = datetime.now()
        db.flush()
        return list(rows)
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return []


def ack(db: Session, msg_id: int, ok: bool = True, error: str = "") -> None:
    m = db.get(AgentMessage, msg_id)
    if not m:
        return
    m.status = "ack" if ok else "failed"
    m.error = (error or "")[:255] or None
    db.flush()


def thread(db: Session, thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """回放一个 thread 的全部消息——这是"多 Agent 协作"最直接的证据。"""
    try:
        rows = db.execute(select(AgentMessage).where(
            AgentMessage.thread_id == thread_id).order_by(
            AgentMessage.id.asc()).limit(limit)).scalars().all()
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return []
    return [{"id": m.id, "parent_id": m.parent_id,
             "from": m.from_agent, "to": m.to_agent,
             "type": m.msg_type, "type_label": contract.describe(m.msg_type),
             "payload": m.payload, "status": m.status,
             "created_at": m.created_at.isoformat(timespec="seconds")
             if m.created_at else None} for m in rows]


def pending_count(db: Session, agent_key: str) -> int:
    """积压数：pending + working 都算。

    只数 pending 会让"正在处理"看起来像"没人管"——页面上的队列一下子空了，
    实际是 agent 正拿着消息干活。两个都算才对得上人看到的现象。
    """
    try:
        return int(db.execute(
            select(func.count()).select_from(AgentMessage).where(
                AgentMessage.to_agent == agent_key,
                AgentMessage.status.in_(("pending", "working")))).scalar() or 0)
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return 0


# ---------------------------------------------------------------------------
# 同步等待（第 5 条检验的实现方式）
# ---------------------------------------------------------------------------
def reply_to(db: Session, msg_id: int) -> Optional[AgentMessage]:
    """取对某条消息的第一条回复（`parent_id` 指回来的那条）。"""
    try:
        return db.execute(select(AgentMessage).where(
            AgentMessage.parent_id == int(msg_id)).order_by(
            AgentMessage.id.asc()).limit(1)).scalars().first()
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return None


def await_reply(msg_id: int, timeout_s: float = 5.0,
                poll_ms: int = 100) -> Optional[Dict[str, Any]]:
    """等 ``msg_id`` 的回复，最多 ``timeout_s`` 秒。超时返回 None。

    **每次轮询用独立的短 Session**，不复用调用方那个。原因不是洁癖：
    MySQL 默认 REPEATABLE READ，调用方在发完消息后事务还没提交，
    拿着同一个 Session 反复查永远看到同一个快照——回复明明已经写进去了，
    这里却会一直读到空，最后报"agent 未响应"。这种 bug 只在真并发下出现，
    单进程测试根本复现不了。

    读到回复就把它 ack 掉（人类没有收件箱，没人替它消费）：不 ack 的话
    这条消息会永远 pending，`pump` 时代会被当成"没人处理"、页面上的
    待办数也永远不归零。

    超时的口径：返回 None 由调用方决定说什么。**不要在这里抛 HTTPException**，
    业务层不该知道 HTTP 的存在。
    """
    from ..db import session_as

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        db2 = session_as()
        try:
            row = reply_to(db2, msg_id)
            if row is not None:
                out = {"id": row.id, "parent_id": row.parent_id,
                       "from": row.from_agent, "to": row.to_agent,
                       "type": row.msg_type, "payload": row.payload or {},
                       "status": row.status,
                       "created_at": row.created_at.isoformat(timespec="seconds")
                       if row.created_at else None}
                if row.status in ("pending", "working"):
                    row.status = "ack"
                    row.consumed_at = datetime.now()
                    try:
                        db2.commit()
                    except (ProgrammingError, DatabaseError):
                        db2.rollback()      # 消费标记失败不该让调用方拿不到回复
                return out
        except (ProgrammingError, DatabaseError) as exc:
            _guard(exc)
            return None
        finally:
            db2.close()

        remain = deadline - time.monotonic()
        if remain <= 0:
            return None
        time.sleep(min(max(0.01, poll_ms / 1000.0), remain))


def submit(db: Session, *, user_id: int, to_agent: str, action: str,
           args: Optional[Dict[str, Any]] = None,
           order_id: Optional[int] = None,
           exception_id: Optional[int] = None,
           thread_id: Optional[str] = None,
           timeout_s: Optional[float] = None,
           from_agent: Optional[str] = None) -> Dict[str, Any]:
    """人类委托一个写操作给 Agent，并等它的回复（S5 的唯一写入口径）。

    发出去的是 ``invoke``，回来的是 ``accept`` / ``reject`` / ``notify``：
    - ``accept``/``notify`` → 业务做成了，payload 就是原来服务函数的返回值；
    - ``reject``           → 业务上不行，payload.reason 是人话原因；
    - 等不到               → 抛 ``NoReply``，调用方转 504 并指向 start_agents.bat。

    **投递用 commit=True**：消息必须先落库，否则收件方看不到它，
    调用方却已经在等——5 秒白白耗完。业务写入由收件方自己提交，
    这里不碰任何业务表。
    """
    actor = from_agent or contract.human_key(user_id)
    msg = send(db, from_agent=actor, to_agent=to_agent, msg_type="invoke",
               payload={"action": str(action), "args": dict(args or {})},
               order_id=order_id, exception_id=exception_id,
               thread_id=thread_id, commit=True)
    waited = wait_timeout(timeout_s)
    rep = await_reply(msg.id, timeout_s=waited)
    if rep is None:
        raise NoReply(msg.id, to_agent, waited)
    return {"msg_id": msg.id, "reply_id": rep["id"], "type": rep["type"],
            "payload": rep["payload"], "from": rep["from"]}


def thread_view(db: Session, order_id: int | None = None,
                exception_id: int | None = None) -> Dict[str, Any]:
    """回放一笔业务的全部 Agent 消息——"多 agent 在协作"最直接的证据。"""
    tid = contract.thread_of(order_id=order_id, exception_id=exception_id)
    rows = thread(db, tid)
    return {"thread_id": tid, "count": len(rows), "messages": rows,
            "pending": {k: pending_count(db, k)
                        for k in ("dispatch", "exception")}}


# ---------------------------------------------------------------------------
# 心跳：HTTP 侧据此区分"agent 在忙"和"agent 没启动"
# ---------------------------------------------------------------------------
def touch(db: Session, agent_key: str, pid: Optional[int] = None,
          host: Optional[str] = None, commit: bool = True) -> None:
    try:
        a = db.execute(select(AgentActor).where(
            AgentActor.agent_key == agent_key)).scalars().first()
        if not a:
            return
        a.last_seen_at = datetime.now()
        if pid:
            a.pid = int(pid)
        if host:
            a.host = str(host)[:64]
        if commit:
            db.commit()
        else:
            db.flush()
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)


def heartbeat_age(db: Session, agent_key: str) -> Optional[float]:
    """距上次心跳多少秒。None 表示从没跳过（或表不可用）。"""
    try:
        ts = db.execute(select(AgentActor.last_seen_at).where(
            AgentActor.agent_key == agent_key)).scalar()
    except (ProgrammingError, DatabaseError) as exc:
        _guard(exc)
        return None
    if not ts:
        return None
    return (datetime.now() - ts).total_seconds()


def alive(db: Session, agent_key: str, stale_s: float = ALIVE_STALE_S) -> bool:
    """有没有活着的进程在服务这个 agent_key。"""
    age = heartbeat_age(db, agent_key)
    return age is not None and age <= float(stale_s)


__all__ = ["ALIVE_STALE_S", "BusUnavailable", "NoReply", "register", "utility_of",
           "remember",
           "recall", "memories", "send", "reply", "claim", "ack", "thread",
           "pending_count", "reply_to", "await_reply", "submit", "wait_timeout",
           "thread_view", "touch", "heartbeat_age", "alive"]
