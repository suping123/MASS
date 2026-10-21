"""三端各自的对话 Agent。

不是自主循环 Agent：每个 Agent 都是「确定性状态机 + 规则解析 + 可选模型兜底」，
每次请求只推进一格，轮次由 `SCA_CHAT_MAX_ROUNDS` 封顶。这样可复现、可审计，
也不会出现一个会话跑几十轮把 token 烧光。
"""
from .shipper_agent import ShipperAgent
from .shipper_agent import agent as shipper_agent

__all__ = ["ShipperAgent", "shipper_agent"]
