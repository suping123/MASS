"""检索重排（FR-10 第三阶段）：在混合召回之后做一次"把 query 和文档放在一起看"的打分。

为什么融合分不够
----------------
融合分是 0.6*向量 + 0.4*关键词，两路各自独立打分再加权——它不知道 query 里的词
在文档里**是不是挨在一起**。典型错例：

    query = "冷链断链怎么处置"
    文档 A（长 SOP）：把"冷链""温控""断链""处置"分散在四段，篇幅长、向量相似度高
    文档 B（短条目）："冷链断链后 30 分钟内回温并拍照留证"——词挨着，篇幅短

融合后 A 排第一、B 排第二。但人要的是 B。重排就是修这件事：覆盖率、命中密度、
词项邻近度、字段命中，再叠加首轮分数（不丢掉召回信号，只调整次序）。

三档开关（SCA_RAG_RERANK）
--------------------------
- ``off``：只用融合分，行为与改造前逐位一致（回滚开关）
- ``local``：本地确定性重排，零外部依赖、零网络抖动，结果可复现（默认）
- ``api``：DashScope ``gte-rerank`` 交叉编码；无 Key / 超时 / 报错一律降级 local

**重排绝不允许抛异常**。它在异常处置的关键路径上（查 SOP），挂了就整条链挂；
而它带来的只是"第 2 条变第 1 条"的收益。所以所有外部调用都包在 try 里，
连 import 失败都算降级。
"""
from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .text import terms as _terms

logger = logging.getLogger("sca.rag.rerank")

# 邻近度的"算挨着"窗口：40 字以内视为同一段话。这是业务口径不是调参起点——
# 一条 SOP 的单步描述通常在 20-40 字，跨过 40 字基本就是两段了。
_NEAR_WINDOW = 40.0

# 权重（总和 1.25，最后乘 0.8 归一到 0-1 区间；线性缩放不改变同一 query 内的排序）
_W_COVER, _W_DENSE, _W_NEAR, _W_FIELD, _W_BASE = 0.40, 0.15, 0.25, 0.10, 0.35
_W_TOTAL = _W_COVER + _W_DENSE + _W_NEAR + _W_FIELD + _W_BASE


def mode() -> str:
    """当前重排档位。非法值按 local 处理——配错不该让检索变哑。"""
    v = str(os.environ.get("SCA_RAG_RERANK", "local") or "local").strip().lower()
    return v if v in ("off", "local", "api") else "local"


def _coverage(q: set, content: str) -> float:
    """query 的词项有多少比例在文档里出现过。"""
    if not q:
        return 0.0
    return len(q & _terms(content)) / len(q)


def _density(q: set, content: str) -> float:
    """命中词项在文档里的**富集度**：命中次数 ÷ 文档长度，再做饱和映射。

    不除以长度的话，长文只要出现字数够多就赢——正好和重排的目的相反。
    饱和映射（而不是线性）是为了让"命中三次"和"命中五次"不再拉开差距：
    超过一定富集度后，再多出现几遍并不让它更对口。
    """
    from .text import tokenize

    toks = tokenize(content)
    if not toks:
        return 0.0
    hits = sum(1 for t in toks if t in q)
    if hits <= 0:
        return 0.0
    return 1.0 - math.exp(-(hits / len(toks)) * 12.0)


def _proximity(q: Sequence[str], content: str) -> float:
    """query 词项在文档中的最小覆盖跨度——越紧凑越像"一句话"，越可能是对的口径。

    只在出现过的词项上算：没出现的不参与（那由覆盖率扣分），否则"只命中一个词
    但恰好位于开头"会拿到虚高的邻近度。
    """
    pos: Dict[str, int] = {}
    for t in q:
        i = content.find(t)
        if i >= 0:
            pos[t] = i
    if len(pos) < 2:
        return 0.0
    idx = sorted(pos.values())
    span = float(idx[-1] - idx[0] + 2)
    return 1.0 / (1.0 + span / _NEAR_WINDOW)


def _field(query: str, meta: Dict[str, Any]) -> float:
    """结构化字段命中：故障码、SOP 编号、类目名。

    这几个字段是人工标注的高置信信号——"E-03"出现在 query 里、"E-03"就是这条
    文档的 code，这种命中比正文里凑巧出现两个字可靠得多。
    """
    if not meta:
        return 0.0
    q = (query or "").lower()
    for key in ("code", "sop_id", "title", "category", "name"):
        val = str(meta.get(key) or "").strip().lower()
        if val and val in q:
            return 1.0
    return 0.0


def score(query: str, content: str, meta: Optional[Dict[str, Any]] = None,
          base: float = 0.0) -> float:
    """单条文档的重排分。`base` 是首轮融合分（0-1）。"""
    q_list = list(_terms(query))
    q_set = set(q_list)
    if not q_set:
        return round(float(base), 4)
    raw = (_W_COVER * _coverage(q_set, content)
           + _W_DENSE * _density(q_set, content)
           + _W_NEAR * _proximity(q_list, content)
           + _W_FIELD * _field(query, meta or {})
           + _W_BASE * max(0.0, min(1.0, float(base))))
    return round(raw / _W_TOTAL, 4)


# ---------------------------------------------------------------------------
# 外部交叉编码重排（DashScope gte-rerank）
# ---------------------------------------------------------------------------
def _api_rerank(query: str, docs: List[str], top_k: int) -> Optional[List[Tuple[int, float]]]:
    """调 gte-rerank。返回 [(原下标, 相关性分)]；任何异常返回 None 由调用方降级。"""
    from sca.settings import settings

    key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
    if not key or not docs:
        return None
    base = str(getattr(settings, "dashscope_base_url", "") or "").strip() \
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    url = base.rstrip("/")
    # 兼容模式 base 是 .../compatible-mode/v1，rerank 在 .../api/v1 下
    if url.endswith("/compatible-mode/v1"):
        url = url[: -len("/compatible-mode/v1")]
    url += "/api/v1/services/rerank/text-rerank/text-rerank"

    body = json.dumps({
        "model": "gte-rerank-v2",
        "input": {"query": query, "documents": docs},
        "parameters": {"return_documents": False, "top_n": min(top_k, len(docs))},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    # 2 秒：查 SOP 是人等着的动作，重排不该比召回更慢
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: List[Tuple[int, float]] = []
    for item in (data.get("output") or {}).get("results") or []:
        idx = item.get("index")
        if isinstance(idx, int):
            out.append((idx, float(item.get("relevance_score") or 0.0)))
    return out or None


# ---------------------------------------------------------------------------
# 对外两个入口
# ---------------------------------------------------------------------------
def rerank_chunks(query: str, hits: List[Tuple[Any, float]], top_k: int
                  ) -> List[Tuple[Any, float]]:
    """给 `HybridRetriever.search` 用：hits 是 [(Chunk, fused_score)]。"""
    m = mode()
    if m == "off":
        return hits[:top_k]

    if m == "api":
        try:
            got = _api_rerank(query, [str(getattr(c, "content", "")) for c, _ in hits], top_k)
            if got:
                picked = [(hits[i][0], round(s, 4)) for i, s in got if 0 <= i < len(hits)]
                if picked:
                    return picked[:top_k]
        except Exception as exc:            # noqa: BLE001 降级是设计，不是兜底
            logger.warning("rerank api 失败，降级 local：%s", exc)

    scored = []
    for c, base in hits:
        content = str(getattr(c, "content", ""))
        meta = getattr(c, "metadata", None) or {}
        s = score(query, content, meta, base)
        # 噪声过滤：一个词都不沾、结构化字段也没对上、首轮分还不高 -> 不进结果。
        # 只在 local 档做：api 档由交叉编码模型自己判断相关性。
        # 保留 base >= 0.5 的条目是有意的——那可能是"用词不同但语义相近"的真召回，
        # 纯词面过滤会把它误杀，而这正是向量召回存在的意义。
        if _coverage(set(_terms(query)), content) == 0.0 and _field(query, meta) == 0.0 \
                and float(base) < 0.5:
            continue
        scored.append((c, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def rerank_rows(query: str, rows: List[Tuple[str, float, Dict]], top_k: int
                ) -> List[Tuple[str, float, Dict]]:
    """给 `pg_vector_search` 用：rows 是 [(doc_id, fused_score, metadata)]。

    metadata 里必须带 ``content``（正文）。PG 回填时把 content 放进 meta，
    这是重排的前提——没有正文只能按融合分原样返回，等于没重排。
    """
    m = mode()
    if m == "off":
        return rows[:top_k]

    if m == "api":
        try:
            got = _api_rerank(query, [str(r[2].get("content") or "") for r in rows], top_k)
            if got:
                picked = [(rows[i][0], round(s, 4), rows[i][2])
                          for i, s in got if 0 <= i < len(rows)]
                if picked:
                    return picked[:top_k]
        except Exception as exc:            # noqa: BLE001
            logger.warning("rerank api 失败，降级 local：%s", exc)

    scored = []
    for doc_id, base, meta in rows:
        meta = meta or {}
        content = str(meta.get("content") or "")
        s = score(query, content, meta, base)
        if _coverage(set(_terms(query)), content) == 0.0 and _field(query, meta) == 0.0 \
                and float(base) < 0.5:
            continue                    # 噪声，理由同 rerank_chunks
        scored.append((doc_id, s, meta))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


__all__ = ["mode", "score", "rerank_chunks", "rerank_rows"]
