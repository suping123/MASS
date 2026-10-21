"""真实 Key 验证脚本（需先 pip install openai）。

验证两件事：
  1) tongyi-intent-detect-v3 意图识别（openai AsyncOpenAI，OpenAI 兼容端点）
  2) text-embedding-v3 向量生成

用法（在已激活 .venv 后）：
  pip install openai
  python scripts/verify_key.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# 让脚本能 import 到 sca 包
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sca.settings import settings  # noqa: E402


async def test_intent() -> None:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        print("[intent] 未安装 openai，跳过（请先 pip install openai）")
        return

    if not settings.dashscope_api_key:
        print("[intent] 未配置 SCA_DASHSCOPE_API_KEY，跳过")
        return

    client = AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )
    tools = [
        {"type": "function", "function": {
            "name": "order.query", "description": "查询运单状态",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "plan.replan", "description": "重规划路径",
            "parameters": {"type": "object", "properties": {}}}},
    ]
    system = (
        "You are Qwen, created by Alibaba Cloud. You are a helpful assistant. "
        "You may call one or more tools to assist with the user query. "
        f"The tools you can use are as follows:\n{tools}\nResponse in INTENT_MODE."
    )
    resp = await client.chat.completions.create(
        model=settings.intent_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "客户希望这批生鲜今天必须送到，怎么调度比较稳妥"},
        ],
        timeout=10,
    )
    content = resp.choices[0].message.content or ""
    print("[intent] 模型返回：", content[:400])


async def test_embedding() -> None:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        print("[embed]  未安装 openai，跳过")
        return

    if not settings.dashscope_api_key:
        print("[embed]  未配置 SCA_DASHSCOPE_API_KEY，跳过")
        return

    client = AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input="冷链失效立即挂起审批并通知收货方",
    )
    dims = len(resp.data[0].embedding)
    print(f"[embed]  向量维度 = {dims}，模型 = {settings.embedding_model}")


async def main() -> None:
    print("=== 真实 Key 验证（DashScope）===")
    print(f"base_url   = {settings.dashscope_base_url}")
    print(f"intent     = {settings.intent_model}")
    print(f"embedding  = {settings.embedding_model}")
    print(f"key 已配置 = {'是' if settings.dashscope_api_key else '否'}")
    await test_intent()
    await test_embedding()
    print("=== 完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
