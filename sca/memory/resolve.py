"""冲突消解（纯函数）：提案 + 现状 -> 裁定 + 原因码。

为什么不许 LLM 投票裁决事实冲突：投票不可回放、不可单测、不可审计。
这里全部是确定性规则，每个裁定都带 reason 码进 memory_ops，事后可复盘
"为什么这条记忆没进库/为什么旧值被覆盖"。

规则（按序）：
  1. delete 对不存在的键            -> SKIP   noop_delete_absent
  2. 与现存有效条目同值（内容哈希）  -> SKIP   dedup_same_value
  3. inferred 且置信度 < 下限        -> CONTESTED low_confidence
  4. inferred 提案 vs 现存 explicit  -> CONTESTED inferred_vs_explicit
  5. 其余（含 explicit 覆盖 explicit）-> KEEP   upsert / delete_valid

explicit 覆盖 explicit 是正常业务（"在北京" -> "搬去上海了"）：KEEP，
由 store 落库时把旧版置 superseded（valid_to=now），历史不丢。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .models import (CONFIDENCE_FLOOR, OP_DELETE, SOURCE_EXPLICIT,
                     SOURCE_INFERRED, MemoryItem, MemoryOp, content_hash)

# 裁定去向
KEEP = "keep"            # 交 Committer 执行
SKIP = "skip"            # 不执行（dedup / noop）
CONTESTED = "contested"  # 不执行，标记待澄清（调用方可据此反问用户）


def resolve(
    proposed: List[MemoryOp],
    existing: Dict[str, MemoryItem],
) -> List[Tuple[MemoryOp, str, str]]:
    """返回 [(op, decision, reason)]，与入参等长、同序。"""
    out: List[Tuple[MemoryOp, str, str]] = []
    for op in proposed:
        cur = existing.get(op.key)
        if op.action == OP_DELETE:
            out.append((op, SKIP, "noop_delete_absent") if cur is None
                       else (op, KEEP, "delete_valid"))
            continue
        if cur is not None and cur.content_hash == content_hash(op.value):
            out.append((op, SKIP, "dedup_same_value"))
            continue
        if op.source == SOURCE_INFERRED and op.confidence < CONFIDENCE_FLOOR:
            out.append((op, CONTESTED, "low_confidence"))
            continue
        if (op.source == SOURCE_INFERRED and cur is not None
                and cur.source == SOURCE_EXPLICIT):
            out.append((op, CONTESTED, "inferred_vs_explicit"))
            continue
        out.append((op, KEEP, "upsert"))
    return out
