"""站内通知接口（三端共用）。

任何已登录用户只能看到**自己的**通知——收件人隔离靠 `user_id` 过滤，
不靠角色判断（同一角色有多个用户）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import current_user
from ..services import notify as notify_svc

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(unread_only: bool = Query(default=False),
                       limit: int = Query(default=30, ge=1, le=100),
                       user: Dict[str, Any] = Depends(current_user),
                       db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = notify_svc.inbox(db, user["user_id"], limit=limit,
                            unread_only=unread_only)
    return {"items": rows, "unread": notify_svc.unread_count(db, user["user_id"])}


@router.get("/unread")
def unread(user: Dict[str, Any] = Depends(current_user),
           db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"unread": notify_svc.unread_count(db, user["user_id"])}


@router.post("/{nid}/read")
def read_one(nid: int, user: Dict[str, Any] = Depends(current_user),
             db: Session = Depends(get_db)) -> Dict[str, Any]:
    n = notify_svc.mark_read(db, user["user_id"], nid)
    return {"updated": n, "unread": notify_svc.unread_count(db, user["user_id"])}


@router.post("/read-all")
def read_all(user: Dict[str, Any] = Depends(current_user),
             db: Session = Depends(get_db)) -> Dict[str, Any]:
    n = notify_svc.mark_read(db, user["user_id"], None)
    return {"updated": n, "unread": 0}
