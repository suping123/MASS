"""收件箱：带租约的消息领取。

为什么不能沿用 v5 的 `claim()`
------------------------------
v5 的 `claim()` 一次事务里把所有发给自己的 pending 改成 `consumed`，然后由同一进程
在同一个请求里处理完。那套跑得通，是因为**只有一个进程**。

S4 起四个 agent 是独立进程，各自随时可能被杀、重启、或者被人工开两个：
- 取走就标 consumed → 进程中途挂掉，消息永远不会再被处理，队列看起来是空的，
  业务却是断的；
- 两个进程同时领 → 同一条消息被处理两次，报价会翻倍、通知会重复。

所以这里换成标准的租约模型：

    pending --claim--> working --ack--> ack / failed
                         |
                    (租约到期，进程已死)
                         v
                      pending（可被重领）

领取语句用 ``SELECT ... FOR UPDATE SKIP LOCKED``：并发领取时后到的进程直接跳过
被锁定的行，不会阻塞、也不会重复拿到同一条。这是 MySQL/PostgreSQL 都支持的
行锁语义，不需要额外的分布式锁。

**强度如实**：租约只能保证"不会被两个进程同时处理"，保证不了"处理恰好一次"。
handler 必须是幂等的（本项目的写操作都按业务键判重：同一 offer 只有一个 bid 行、
同一 tender 只有一个 winner），所以至多一次的效果等价于恰好一次。
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import AgentMessage

# 一条消息被领几次还失败就判死。没有这个上限，一条必炸的消息会把
# agent 的收件箱堵死——每轮都先领到它、每轮都失败，后面的消息永远轮不到。
MAX_TRIES = 5

DEFAULT_LEASE_S = 60

_NONCE = uuid.uuid4().hex[:6]
_HOST = socket.gethostname()[:32]


def owner_of(agent_key: str) -> str:
    """进程级唯一标识：``{agent_key}@{host}:{pid}#{nonce}``。

    nonce 是进程启动时生成的，同一个进程重启后 PID 可能复用，
    没有 nonce 就分不清"同一个人续租"和"新进程抢到了旧租约"。
    """
    return f"{agent_key}@{_HOST}:{os.getpid()}#{_NONCE}"


def _whole_second(ts: datetime) -> datetime:
    """截掉微秒。租约时间要落 DATETIME(0) 列，带微秒会被四舍五入进位。"""
    return ts.replace(microsecond=0)


def claim(db: Session, agent_key: str, *, limit: int = 10,
          lease_s: int = DEFAULT_LEASE_S,
          owner: Optional[str] = None,
          now: Optional[datetime] = None) -> List[AgentMessage]:
    """领一批消息并加租约。**函数内部会 commit**，调用方不要再包事务。

    领取条件：状态是 pending，或是 working 但租约已过期（进程已死）。
    """
    now = now or datetime.now()
    owner = owner or owner_of(agent_key)

    stmt = (select(AgentMessage).where(
        AgentMessage.to_agent == agent_key,
        or_(
            AgentMessage.status == "pending",
            and_(AgentMessage.status == "working",
                 AgentMessage.lease_until.isnot(None),
                 AgentMessage.lease_until < now),
        )).order_by(AgentMessage.id.asc()).limit(int(limit))
        .with_for_update(skip_locked=True))

    rows = list(db.execute(stmt).scalars().all())
    out: List[AgentMessage] = []
    for m in rows:
        tries = int(m.tries or 0)
        if tries >= MAX_TRIES:
            m.status = "failed"
            m.error = (m.error or f"超过最大重试次数 {MAX_TRIES}")[:255]
            m.lease_owner = None
            m.lease_until = None
            continue
        m.status = "working"
        m.lease_owner = owner
        # 先截掉微秒再算：`lease_until` 是 DATETIME(0)，写库时**微秒会四舍五入**，
        # `now + 1s` 里的 .5 秒会被进位成 +2s —— 于是"1 秒租约"实际能长到 1.5 秒，
        # "60 秒租约"能长到 60.5 秒。租约是给"进程死了多久才被别人接手"兜底的，
        # 只应该等于申请的秒数，不该悄悄多给半秒。
        m.lease_until = _whole_second(now) + timedelta(seconds=max(1, int(lease_s)))
        m.tries = tries + 1
        out.append(m)
    db.commit()          # 立刻释放行锁：拿着锁做业务会拖垮其他 agent 的领取
    return out


def renew(db: Session, m: AgentMessage, *, lease_s: int = DEFAULT_LEASE_S,
          owner: Optional[str] = None) -> bool:
    """续租。只有租约仍在自己手上才能续——被抢走的租约不能靠续租抢回来。"""
    db.refresh(m)
    me = owner or owner_of(m.to_agent)
    if m.status != "working" or (m.lease_owner and m.lease_owner != me):
        return False
    m.lease_until = _whole_second(datetime.now()) + timedelta(seconds=max(1, int(lease_s)))
    db.flush()
    return True


def release(db: Session, m: AgentMessage, *, reason: str = "") -> None:
    """退回队列。用于"这条消息现在不该我处理"（状态已变、缺少上下文）。"""
    m.status = "pending"
    m.lease_owner = None
    m.lease_until = None
    m.consumed_at = None
    if reason:
        m.error = reason[:255]
    db.flush()


def finish(db: Session, m: AgentMessage, ok: bool = True,
           error: str = "") -> None:
    """收口。``ok=False`` 且未超重试次数时退成 pending，让下一轮再试一次；
    超过上限才判 failed——一次失败不等于这条消息永远做不成（数据库死锁、
    临时连不上都会失败，下一次就好了）。"""
    tries = int(m.tries or 0)
    if not ok and tries < MAX_TRIES:
        release(db, m, reason=error or "处理失败，等待重试")
        return
    m.status = "ack" if ok else "failed"
    m.error = (error or "")[:255] or None
    m.lease_owner = None
    m.lease_until = None
    m.consumed_at = datetime.now()
    db.flush()


def depth(db: Session, agent_key: str) -> int:
    """未处理完的消息数（pending + working）。给页面显示"这个 agent 积压多少"。"""
    from sqlalchemy import func

    return int(db.execute(
        select(func.count()).select_from(AgentMessage).where(
            AgentMessage.to_agent == agent_key,
            AgentMessage.status.in_(("pending", "working")))).scalar() or 0)


__all__ = ["MAX_TRIES", "DEFAULT_LEASE_S", "owner_of", "claim", "renew",
           "release", "finish", "depth"]
