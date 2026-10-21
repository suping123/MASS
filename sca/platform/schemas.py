"""接口的请求/响应模型（Pydantic v2）。

金额统一以字符串或数字输出均可，但计算一律在服务层用 Decimal，
响应里转成 float 只用于展示，不参与再计算。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CodeReq(BaseModel):
    account: Optional[str] = Field(default=None, max_length=64)
    purpose: str = Field(pattern="^(register|login)$")


class RegisterReq(BaseModel):
    account: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(shipper|dispatcher|carrier)$")
    org_name: str = Field(min_length=2, max_length=128,
                          description="货主写自己公司名，承运商写车队/公司名，调度员写所属平台")
    phone: Optional[str] = Field(default=None, max_length=32)
    code: str = Field(min_length=4, max_length=8, description="图形验证码内容")
    cid: Optional[str] = Field(default=None, max_length=64,
                               description="验证码图片 ID，与图片一起下发")


class LoginReq(BaseModel):
    account: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=8, description="图形验证码内容")
    cid: Optional[str] = Field(default=None, max_length=64,
                               description="验证码图片 ID，与图片一起下发")


class LoginResp(BaseModel):
    token: str
    user: Dict[str, Any]
    home: str = Field(description="该角色进入的前端页面")


class ChatReq(BaseModel):
    session_id: Optional[str] = Field(default=None, description="为空则新建会话")
    text: str = Field(min_length=1, max_length=2000)


class CancelReq(BaseModel):
    reason: str = Field(min_length=2, max_length=500,
                        description="取消原因（必填，会进系统日志）")


class ChatResp(BaseModel):
    session_id: str
    state: str
    reply: str
    slots: Dict[str, Any]
    missing: List[str]
    rounds: int
    done: bool = False
    order_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
