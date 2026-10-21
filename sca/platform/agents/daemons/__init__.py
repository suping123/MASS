"""四个常驻 Agent 的注册表。

这里只做路由，不含任何业务判断：

    agent_key  --kind_of-->  kind  --查表-->  对应 daemon 模块
    msg_type   ------------>  该模块的 HANDLERS[msg_type]

**为什么不在这里统一写 if/elif**：一旦路由里能塞业务分支，四个 agent 迟早会被
"顺手"合到一个函数里——那就退回单进程单控制了。这里保持"只查表、只转发"。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Type

from sqlalchemy.orm import Session

from ..contract import kind_of
from ..daemon import AgentDaemon, Handler
from . import carrier_daemon, dispatch_daemon, exception_daemon, shipper_daemon

MODULES = {
    "shipper": shipper_daemon,
    "dispatch": dispatch_daemon,
    "carrier": carrier_daemon,
    "exception": exception_daemon,
}

KINDS = tuple(MODULES)


def module_for(kind_or_key: str):
    kind = kind_of(kind_or_key)
    mod = MODULES.get(kind)
    if mod is None:
        raise KeyError(f"没有 kind={kind!r} 的 daemon（可用：{', '.join(KINDS)}）")
    return mod


def handlers_for(agent_key: str) -> Dict[str, Handler]:
    return dict(module_for(agent_key).HANDLERS)


def handle(db: Session, agent_key: str, msg) -> Optional[Dict[str, Any]]:
    """同步处理一条消息（不领取、不 ack）。

    留给测试用：想把一封消息的处理器单独跑一遍时用。**生产路径不经过这里**——
    生产是 ``worker`` 里各进程自己的循环，走这里就等于把四个 agent 又塞回一个
    控制流。正因如此，S5 之后原来的 ``pump.py`` 已经删掉，没有 HTTP 入口。
    """
    fn = handlers_for(agent_key).get(msg.msg_type)
    if fn is None:
        return None
    return fn(db, msg)


def due_for(agent_key: str, db: Session, keys) -> Dict[str, Any]:
    return module_for(agent_key).due(db, keys)


def build(kind: str, *, once: bool = False,
          only_keys: Optional[list] = None) -> AgentDaemon:
    """按 kind 造一个 daemon 实例（worker 用）。"""
    return module_for(kind).Daemon(once=once, only_keys=only_keys)


__all__ = ["MODULES", "KINDS", "module_for", "handlers_for", "handle",
           "due_for", "build"]
