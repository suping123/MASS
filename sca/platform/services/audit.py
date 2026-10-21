"""系统操作日志。

只做两件事：写一条、查一批。**不做更新与删除**——审计日志能被改就失去意义了。
清理（如需要）只能由超管走单独的归档流程，且必须留下"清理了日志"这一条日志。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SysAuditLog


def log(db: Session, actor: Dict[str, Any], action: str, *,
        target_type: Optional[str] = None, target_id: Any = None,
        reason: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
        ip: Optional[str] = None, agent_key: Optional[str] = None) -> SysAuditLog:
    """写一条操作日志。调用方必须在同一事务里 commit，保证"操作与留痕同生共死"。

    ``agent_key`` 标明这次操作是哪个 agent 做的（``carrier:12`` / ``dispatch`` …），
    人工操作留空。没有它，事后只能从 actor 反推"是 agent 还是人点的按钮"，
    而 agent 用同一个账号执行时根本推不出来。
    """
    row = SysAuditLog(
        actor_user_id=int(actor.get("user_id") or 0),
        actor_account=str(actor.get("account") or "")[:64] or None,
        actor_role=str(actor.get("role") or "")[:16],
        agent_key=(str(agent_key)[:48] if agent_key else None),
        action=action[:48],
        target_type=(target_type or None) and str(target_type)[:24],
        target_id=(str(target_id) if target_id is not None else None),
        reason=(str(reason)[:500] if reason else None),
        detail=detail or {},
        ip=(str(ip)[:64] if ip else None),
    )
    db.add(row)
    db.flush()
    return row


def query(db: Session, *, action: Optional[str] = None,
          target_type: Optional[str] = None, actor_role: Optional[str] = None,
          actor_id: Optional[int] = None, keyword: Optional[str] = None,
          limit: int = 100) -> List[Dict[str, Any]]:
    q = select(SysAuditLog)
    if action:
        q = q.where(SysAuditLog.action == action)
    if target_type:
        q = q.where(SysAuditLog.target_type == target_type)
    if actor_role:
        q = q.where(SysAuditLog.actor_role == actor_role)
    if actor_id:
        q = q.where(SysAuditLog.actor_user_id == actor_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.where(SysAuditLog.reason.like(like) | SysAuditLog.target_id.like(like)
                    | SysAuditLog.actor_account.like(like))
    rows = db.execute(q.order_by(SysAuditLog.id.desc()).limit(limit)).scalars().all()
    return [{
        "id": r.id, "at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "actor": r.actor_account, "role": r.actor_role, "agent_key": r.agent_key,
        "action": r.action,
        "target_type": r.target_type, "target_id": r.target_id,
        "reason": r.reason, "detail": r.detail, "ip": r.ip,
    } for r in rows]


def distinct_actions(db: Session, limit: int = 60) -> List[str]:
    rows = db.execute(select(SysAuditLog.action).distinct()
                      .order_by(SysAuditLog.action).limit(limit)).all()
    return [r[0] for r in rows]
