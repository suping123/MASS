"""端到端真实意图链路测试（零依赖模式，走标准库直连 DashScope）。

用一句不含业务规则关键词的话，迫使模型兜底，验证 tongyi-intent-detect-v3 真实生效。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sca.intent import build_intent_model  # noqa: E402


async def main() -> None:
    model = build_intent_model()
    tools = [
        {"name": "order.query", "description": "查询运单状态"},
        {"name": "plan.replan", "description": "重规划路径"},
        {"name": "capacity.match", "description": "匹配运力"},
    ]
    # 不含规则关键词，强制走模型兜底
    text = "客户希望这批生鲜今天必须送到，怎么调度比较稳妥"
    res = await model.classify(text, tools, {})
    print("intent  =", res.intent)
    print("source  =", res.source)
    print("conf    =", res.confidence)
    print("calls   =", res.tool_calls)


if __name__ == "__main__":
    asyncio.run(main())
