"""站内通知：把"发生在别人端上的事"落到收件人的消息列表里。

改造前的缺口
------------
平台完全没有通知落库机制：调度改了承诺时效、改了承运商、异常上报，货主端只有
刷新页面才可能发现。Agent 说的"已通知货主"实际上只是它自己的回复文本——
对方根本收不到。

这里只做一件事：写一条通知。不负责推送外部渠道（钉钉走 notify-mcp，那是内核
链路的事）。写失败要显性暴露，不能因为"通知失败"就把业务动作静默回滚——
反过来也一样：业务失败就不该写通知，所以调用方应当在业务成功之后再调用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Notification, SysUser

# 通知类型 -> 中文标签（页面直接显示，不必再维护一份映射）
NTYPE_LABEL = {
    "deadline_changed": "时效变更",
    "reassigned": "承运商变更",
    "exception": "异常上报",
    "plan": "方案更新",
    "reroute": "改道",
    "tender": "招标",
    "negotiation": "价格协商",
}


def push(db: Session, user_id: Optional[int], *, ntype: str, title: str,
         body: str = "", order_id: Optional[int] = None) -> Optional[Notification]:
    """写一条通知。`user_id` 为空时静默跳过（该角色没有登录账号）。"""
    if not user_id:
        return None
    n = Notification(user_id=int(user_id), order_id=order_id, ntype=ntype,
                     title=title[:128], body=body or None)
    db.add(n)
    db.flush()
    return n


def push_order(db: Session, order, *, ntype: str, title: str, body: str = "",
               to_shipper: bool = True, to_carrier: bool = True) -> List[int]:
    """按订单把通知推给货主与承运商。返回写入的通知 id。"""
    ids: List[int] = []
    if to_shipper and getattr(order, "shipper_user_id", None):
        n = push(db, order.shipper_user_id, ntype=ntype, title=title, body=body,
                 order_id=order.id)
        if n:
            ids.append(n.id)
    if to_carrier and getattr(order, "carrier_user_id", None):
        n = push(db, order.carrier_user_id, ntype=ntype, title=title, body=body,
                 order_id=order.id)
        if n:
            ids.append(n.id)
    return ids


def inbox(db: Session, user_id: int, limit: int = 30,
          unread_only: bool = False) -> List[Dict[str, Any]]:
    q = select(Notification).where(Notification.user_id == int(user_id))
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q.order_by(Notification.id.desc()).limit(limit)).scalars().all()
    return [{"id": r.id, "order_id": r.order_id, "ntype": r.ntype,
             "type_label": NTYPE_LABEL.get(r.ntype, r.ntype),
             "title": r.title, "body": r.body,
             "read": r.read_at is not None,
             "created_at": r.created_at.isoformat(timespec="seconds")
             if r.created_at else None} for r in rows]


def unread_count(db: Session, user_id: int) -> int:
    from sqlalchemy import func

    return int(db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == int(user_id),
        Notification.read_at.is_(None))).scalar() or 0)


def mark_read(db: Session, user_id: int, nid: Optional[int] = None) -> int:
    """标记已读。`nid` 为空表示全部已读。返回本次更新条数。"""
    from datetime import datetime

    q = select(Notification).where(Notification.user_id == int(user_id),
                                   Notification.read_at.is_(None))
    if nid:
        q = q.where(Notification.id == int(nid))
    rows = db.execute(q).scalars().all()
    now = datetime.now()
    for r in rows:
        r.read_at = now
    db.commit()
    return len(rows)


__all__ = ["NTYPE_LABEL", "push", "push_order", "inbox", "unread_count", "mark_read"]
