"""人类闸口：HTTP 只投一条消息，执行交给 Agent 进程（S5）。

为什么要有这个文件
------------------
S5 之前，`/accept` 直接调 `carrier_ops.accept()`。于是"四个独立 agent"只在
招标那半条链上成立：关掉全部 agent 进程，下单→挂牌→接单→送达照样能走完，
四个进程纯属摆设。第 5 条检验（移除交互）过不了，原因就在这里。

S5 之后所有成交类写操作都长这样::

    路由 --invoke--> Agent 收件箱 --(daemon 消费)--> 服务函数 --回复--> 路由

失败只有两种，必须分开说：
- 业务不行（有回复 `reject`）→ 409，把 `payload.reason` 原样回给用户；
- 没人接活（`NoReply`）      → 504，指向 start_agents.bat。

**这个文件里不许有业务判断**。一旦在这里写"如果订单状态是 X 就……"，
路由和 daemon 就会各有一套规则，且迟早不一致。委托机制在 `agents/handoff.py`，
这里只做"消息 → HTTP 状态码"的翻译。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..agents import bus, contract, handoff

# 供路由判角色用（不在本文件做业务判断，只是转发）
target_of = handoff.agent_key_of
operator_of = handoff.operator


def invoke(db: Session, user: Dict[str, Any], *, action: str,
           args: Optional[Dict[str, Any]] = None,
           to_agent: Optional[str] = None,
           order_id: Optional[int] = None,
           exception_id: Optional[int] = None,
           thread_id: Optional[str] = None,
           timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """投一条 ``invoke`` 并等回复。返回 ``{type, payload, msg_id, ...}``。"""
    try:
        return handoff.send(db, user, action=action, args=args,
                            to_agent=to_agent, order_id=order_id,
                            exception_id=exception_id, thread_id=thread_id,
                            timeout_s=timeout_s)
    except bus.NoReply as exc:
        raise HTTPException(status_code=504, detail=(
            f"{exc}。请确认 {exc.to_agent} 这一侧的 agent 进程在运行："
            f"双击 start_agents.bat，或 python -m sca.platform.agents.worker "
            f"--kind {contract.kind_of(exc.to_agent)}")) from None
    except bus.BusUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except contract.ContractError as exc:
        # 走到这里说明是系统自己发错了消息，不是用户的问题
        raise HTTPException(status_code=500,
                            detail=f"消息契约不成立：{exc}") from None


def unwrap(r: Dict[str, Any]) -> Dict[str, Any]:
    """把 Agent 的回复翻成"成功返回值"或 409。

    返回结构刻意与改造前的服务函数一致（订单对象 / 挂牌结果），
    这样页面和已有断言都不用改——改造只换执行者，不换口径。
    """
    try:
        return handoff.result(r)
    except handoff.HandoffError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


__all__ = ["target_of", "operator_of", "invoke", "unwrap"]
