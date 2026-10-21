"""长短期记忆子系统（sca.memory）。

对外唯一入口。接线约定（api.py 已按此挂钩）：
  轮开始：await memory.safe_recall_blocks(session_id, user_id, query=text)
          -> 注入 LangGraph 初始状态 memory_blocks（读前）
  轮结束：await memory.safe_observe_turn(session_id, user_id, text)
          -> 抽取 + Guard + Resolver + commit（轮后，异步写）

fail-open 是硬约定：记忆故障只 warn + 计数，绝不把调度主流程带崩
（降级路径：跳过记忆注入/跳过本轮写入，对话照常进行）。

依赖：SCA_USE_PG=true 作持久层（PG 不可用时整个记忆子系统关闭）；
SCA_USE_REDIS 控制热层 + WAL（不可达自动降级 pg_direct）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sca.settings import settings

from .context import build_memory_blocks, render_memory_prompt
from .extract import RuleExtractor, guard
from .models import MemoryItem, MemoryOp
from .resolve import resolve
from .store import MemoryStore

logger = logging.getLogger("sca.memory")

_store: Optional[MemoryStore] = None


def enabled() -> bool:
    """记忆子系统总闸：显式关闭或缺 PG 持久层时整体停用。"""
    return bool(settings.memory_enabled and settings.use_pg and settings.pg_dsn)


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


# ------------------------------------------------------------------ 写
async def observe_turn(session_id: str, user_id: str, text: str,
                       actor: str = "用户") -> Dict[str, Any]:
    """观察一轮输入：抽取 -> Guard -> Resolver -> commit。

    返回执行摘要（含 mode / committed / contested / skipped），供测试与观测。
    """
    if not enabled():
        return {"skipped": "memory_disabled"}
    store = get_store()
    raw = RuleExtractor().propose(text)
    kept, dropped = guard(raw)
    for op in dropped:
        logger.info("[memory] 丢弃提案 key=%s reason=%s", op.key, op.reason)
    if not kept:
        return {"mode": await store.mode(), "committed": [], "contested": 0,
                "skipped": 0, "dropped": len(dropped)}
    existing = {i.key: i for i in await store.recall_items(user_id)}
    resolved = resolve(kept, existing)
    out = await store.commit(user_id, resolved, existing, session_id)
    out["dropped"] = len(dropped)
    out["contested"] = sum(1 for _, d, _ in resolved if d == "contested")
    out["skipped"] = sum(1 for _, d, _ in resolved if d == "skip")
    return out


# ------------------------------------------------------------------ 读
async def recall_blocks(session_id: str, user_id: str,
                        query: str = "") -> List[Dict[str, Any]]:
    """召回并组装本轮可注入的记忆块（读前调用）。"""
    if not enabled():
        return []
    store = get_store()
    items = await store.recall_items(user_id)
    return build_memory_blocks(items, budget=settings.memory_context_budget,
                               pinned_csv=settings.memory_pinned_keys)


async def recall_prompt(session_id: str, user_id: str, query: str = "") -> str:
    blocks = await recall_blocks(session_id, user_id, query)
    return render_memory_prompt(blocks)


# ------------------------------------------------------------------ 运维
async def fold_once(count: int = 100) -> Dict[str, Any]:
    if not enabled():
        return {"folded": 0, "note": "memory_disabled"}
    return await get_store().fold_once(count)


async def export_user(user_id: str) -> Dict[str, Any]:
    return await get_store().export_user(user_id)


async def erase_user(user_id: str, actor: str = "用户") -> Dict[str, Any]:
    return await get_store().erase_user(user_id, actor=actor)


async def history(user_id: str, key: Optional[str] = None) -> Dict[str, Any]:
    return await get_store().history(user_id, key)


def stats() -> Dict[str, Any]:
    s = get_store()
    return {"enabled": enabled(), "store_stats": s.stats}


# ------------------------------------------------------------------ fail-open 包装
async def safe_observe_turn(session_id: str, user_id: str, text: str) -> Optional[Dict[str, Any]]:
    """挂钩用：记忆故障只记 warn，不向上抛。"""
    try:
        return await observe_turn(session_id, user_id, text)
    except Exception as exc:
        logger.warning("[memory] observe_turn 失败（不影响主流程）：%s", exc)
        return None


async def safe_recall_blocks(session_id: str, user_id: str,
                             query: str = "") -> List[Dict[str, Any]]:
    """挂钩用：召回故障返回空块，对话降级为无记忆继续。"""
    try:
        return await recall_blocks(session_id, user_id, query)
    except Exception as exc:
        logger.warning("[memory] recall_blocks 失败（降级为无记忆）：%s", exc)
        return []


async def close() -> None:
    if _store is not None:
        await _store.close()
