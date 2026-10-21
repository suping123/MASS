"""委托执行：把"人要做的事"交给对应 Agent 进程，并等它的回复。

S5 之前，接单/挂牌/投标/落单都是「HTTP 请求内直接调服务函数」。那样做，
四个常驻进程只是好看的摆设——把它们全杀掉，业务照样能走完。第 5 条检验
（移除交互）测的就是这件事，所以这几个写操作必须换个执行者。

现在统一成::

    调用方（路由 / 对话 agent） --invoke--> Agent 收件箱 --daemon 消费--> 服务函数
                     ^                                              |
                     +------------------ 回复（accept / reject）----+

为什么单独一个模块而不是塞进 `bus`：`bus` 只管"消息怎么收发"，不认识
"登录用户长什么样"（user_id / org_id / role / account）。把身份拼装放这里，
`bus` 就仍然是一层与业务无关的传输。

为什么不塞进路由：对话 agent（`carrier_agent` / `shipper_agent`）也要走同一条路。
放在路由里，它们就得再抄一遍——抄出来的第二份迟早和第一份不一样。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from . import bus, contract

# 角色 -> Agent 收件箱（承运商按组织分箱）
_ROLE_KIND = {"shipper": "shipper", "carrier": "carrier",
              "dispatcher": "dispatch", "admin": "dispatch"}


class HandoffError(ValueError):
    """业务上被拒绝——**有回复**，`str(exc)` 就是人话原因。"""


def stale(msg: Any, max_age_s: Optional[float] = None) -> bool:
    """这条委托是不是"早就过期了"。

    场景：agent 进程停着的时候，调度员点了挂牌，HTTP 等了 5 秒返回 504，人去处理
    故障了。半小时后进程起来，这条 invoke 还在收件箱里——**不能执行**：人已经认为
    这件事没发生，可能已经换了个价重新挂牌了。迟到的执行比不执行更危险。

    默认 120 秒：比等待上限（5 秒）宽很多，只拦真正"隔了很久"的委托。
    """
    created = getattr(msg, "created_at", None)
    if created is None:
        return False
    if max_age_s is None:
        try:
            max_age_s = float(os.environ.get("SCA_AGENT_STALE_S", "120"))
        except (TypeError, ValueError):
            max_age_s = 120.0
    try:
        age = (datetime.now() - created).total_seconds()
    except (TypeError, ValueError):
        return False
    return age > max_age_s


def operator(user: Dict[str, Any]) -> Dict[str, Any]:
    """把登录用户翻成"谁在操作"，随消息带过去，不让 agent 从会话猜。"""
    return {"user_id": int(user.get("user_id") or 0),
            "org_id": user.get("org_id"), "role": str(user.get("role") or ""),
            "account": str(user.get("account") or "")}


def agent_key_of(user: Dict[str, Any]) -> str:
    """这个角色的 Agent 收件箱。承运商没有归口组织时抛 HandoffError。"""
    role = str(user.get("role") or "")
    kind = _ROLE_KIND.get(role)
    if not kind:
        raise HandoffError(f"角色「{role}」没有对应的 agent")
    if kind == "carrier":
        org = user.get("org_id")
        if not org:
            raise HandoffError("当前账号没有承运商归口组织，无法接单")
        return f"carrier:{int(org)}"
    return kind


def send(db: Session, user: Dict[str, Any], *, action: str,
         args: Optional[Dict[str, Any]] = None,
         to_agent: Optional[str] = None,
         order_id: Optional[int] = None,
         exception_id: Optional[int] = None,
         thread_id: Optional[str] = None,
         timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """投一条 ``invoke`` 并等回复。等不到抛 ``bus.NoReply``。

    身份由这里统一注入 `args["user"]`：让每个调用点各写一遍，
    迟早有一个写漏 `org_id`，那个入口就会在"没有归口组织"上出错。
    """
    payload_args = dict(args or {})
    payload_args["user"] = operator(user)
    return bus.submit(db, user_id=int(user.get("user_id") or 0),
                      to_agent=to_agent or agent_key_of(user),
                      action=action, args=payload_args, order_id=order_id,
                      exception_id=exception_id, thread_id=thread_id,
                      timeout_s=timeout_s)


def result(reply: Dict[str, Any]) -> Dict[str, Any]:
    """取执行结果；被拒绝就抛 ``HandoffError``（带 Agent 给的人话原因）。"""
    if str(reply.get("type") or "") == "reject":
        payload = reply.get("payload") or {}
        raise HandoffError(str(payload.get("reason") or "该操作被 Agent 拒绝"))
    payload = reply.get("payload") or {}
    r = payload.get("result")
    return dict(r) if isinstance(r, dict) else dict(payload)


__all__ = ["HandoffError", "operator", "agent_key_of", "send", "result", "stale"]
