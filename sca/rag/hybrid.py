"""混合检索实现（FR-10）。

两路召回：① 向量语义召回（embedding 由 DashScope text-embedding-v3 生成）；
② 关键词召回（中文 2-gram + 英文词重叠；PG 的 tsvector 'simple' 对中文无效）。
两路结果按 score 融合后交给轻量重排（0.6*向量 + 0.4*关键词 的加权融合近似重排）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("sca.rag")


@dataclass
class Chunk:
    doc_id: str
    content: str
    metadata: Dict = field(default_factory=dict)
    vector: Tuple[float, ...] = ()


def _hash_vec(text: str, dim: int = 32) -> Tuple[float, ...]:
    """离线无 embedding API 时的确定性伪向量（仅用于 demo/校验，非生产语义向量）。"""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals = [((h[i % len(h)] + i) % 251) / 250.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return tuple(v / norm for v in vals)


def _cosine(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _tokenize(text: str) -> List[str]:
    """切词口径统一在 `sca.rag.text`（召回与重排必须用同一个"命中"定义）。"""
    from .text import tokenize
    return tokenize(text)


class HybridRetriever(ABC):
    @abstractmethod
    def add(self, chunks: List[Chunk]) -> None: ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]: ...


class MockHybridRetriever(HybridRetriever):
    """离线内存版：伪向量召回 + 关键词召回 + 加权融合（FR-10 原型验证用）。"""

    def __init__(self, dim: int = 32):
        self.dim = dim
        self._store: List[Chunk] = []

    def add(self, chunks: List[Chunk]) -> None:
        for c in chunks:
            if not c.vector:
                c.vector = _hash_vec(c.content, self.dim)
            self._store.append(c)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]:
        q_vec = _hash_vec(query, self.dim)
        q_toks = set(_tokenize(query))
        scored: List[Tuple[Chunk, float]] = []
        for c in self._store:
            vec_sim = _cosine(q_vec, c.vector)
            c_toks = set(_tokenize(c.content))
            kw = len(q_toks & c_toks) / max(1, len(q_toks)) if q_toks else 0.0
            fused = 0.6 * vec_sim + 0.4 * kw          # 第一阶段：两路融合
            if fused > 0:
                scored.append((c, round(fused, 4)))
        scored.sort(key=lambda x: -x[1])
        # 第二阶段：重排。多留 2 倍候选——只给最终 top_k 重排等于没得选，
        # 融合分排第 4 但语义更对的那条根本进不了重排视野。
        cands = scored[: max(top_k * 2, top_k)]
        try:
            from .rerank import rerank_chunks
            return rerank_chunks(query, cands, top_k)
        except Exception as exc:            # noqa: BLE001 重排不能拖垮检索
            logger.warning("重排不可用，按融合分返回：%s", exc)
            return cands[:top_k]


def _embed_request(texts: List[str]) -> List[List[float]]:
    """DashScope text-embedding-v3（OpenAI 兼容 /embeddings，标准库直连，零依赖）。"""
    import json
    import urllib.request

    from sca.settings import settings
    key = settings.dashscope_api_key
    if not key:
        raise RuntimeError("未配置 SCA_DASHSCOPE_API_KEY，无法生成真实向量")
    url = settings.dashscope_base_url.rstrip("/") + "/embeddings"
    body = {"model": settings.embedding_model, "input": texts}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read().decode("utf-8"))
    data = sorted(j.get("data", []), key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


_EMBED_CACHE: Dict[str, Tuple[Tuple[float, ...], float]] = {}


def embed_query(text: str) -> Optional[Tuple[float, ...]]:
    """查询文本 -> 真实语义向量（内存缓存 10 分钟，控制 API 延迟与额度）。"""
    import time as _time

    if not text.strip():
        return None
    hit = _EMBED_CACHE.get(text)
    if hit and _time.time() - hit[1] < 600:
        return hit[0]
    try:
        vec = tuple(_embed_request([text])[0])
        _EMBED_CACHE[text] = (vec, _time.time())
        return vec
    except Exception as exc:  # noqa
        logger.warning("[rag] 查询向量生成失败：%s", exc)
        return None


def embed_texts(texts: List[str], batch: int = 10) -> List[Optional[Tuple[float, ...]]]:
    """批量生成真实向量（种子脚本用；失败位置返回 None，不阻塞其余文本）。"""
    out: List[Optional[Tuple[float, ...]]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        try:
            vecs = _embed_request(chunk)
            out.extend(tuple(v) for v in vecs)
        except Exception as exc:  # noqa
            logger.warning("[rag] 批量向量生成失败（%s..%s）：%s", i, i + len(chunk), exc)
            out.extend([None] * len(chunk))
    return out


def _vec_literal(vec) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _kw_score(query: str, content: str) -> float:
    """中文 2-gram + 英文词的重叠率（PG tsvector 'simple' 对中文无效，用 Python 侧关键词分）。"""
    q_toks = set(_tokenize(query))
    if not q_toks:
        return 0.0
    c_toks = set(_tokenize(content))
    return len(q_toks & c_toks) / len(q_toks)


# PG 熔断：连接失败后 30 秒内不再尝试。理由：知识库检索在异常处置的关键路径上，
# 每次回退都要付 3 秒连接超时 + 一次 embedding API 往返（约 15 秒、且要花钱），
# 一次故障会让整条处置链卡住半分钟。熔断后立刻走关键词回退，30 秒后再探。
_PG_DOWN_UNTIL: float = 0.0
_PG_PROBE_COOLDOWN = 30.0


def pg_vector_search(query: str, top_k: int = 20,
                     vec_weight: float = 0.6, kw_weight: float = 0.4
                     ) -> List[Tuple[str, float, Dict]]:
    """PG + pgvector 混合检索：向量 ANN 召回 + 关键词重排融合（0.6/0.4）。

    返回 [(doc_id, fused_score, metadata)]。需要 use_pg + 已入库 + embedding 可用；
    任一条件不满足时抛 RuntimeError，由调用方回退关键词检索。
    """
    global _PG_DOWN_UNTIL
    import time as _time

    from sca.settings import settings

    if not (settings.use_pg and settings.pg_dsn):
        raise RuntimeError("PG 未启用（SCA_USE_PG=false）")
    if _time.time() < _PG_DOWN_UNTIL:
        raise RuntimeError("PG 近期连接失败，熔断中（30s 后重试）")

    import psycopg

    # 先连库再算向量：反过来（原先的写法）会在 PG 挂掉时白调一次 embedding API，
    # 15 秒超时还照样失败，等于用最贵的方式发现"库连不上"。
    try:
        conn = psycopg.connect(settings.pg_dsn, connect_timeout=3)
    except Exception as exc:
        _PG_DOWN_UNTIL = _time.time() + _PG_PROBE_COOLDOWN
        raise RuntimeError(f"PG 连接失败：{type(exc).__name__}") from exc

    try:
        qvec = embed_query(query)
        if not qvec:
            raise RuntimeError("查询向量不可用（未配 Key 或 API 失败）")
        rows = conn.execute(
            "SELECT doc_id, content, metadata, 1 - (embedding <=> %s::vector) AS vec_sim "
            "FROM knowledge_chunks WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (_vec_literal(qvec), _vec_literal(qvec), top_k * 3)).fetchall()
    finally:
        conn.close()

    out: List[Tuple[str, float, Dict]] = []
    for doc_id, content, meta, vec_sim in rows:
        meta = dict(meta) if isinstance(meta, dict) else {}
        # 正文放进 meta 是重排的前提：`rerank_rows` 只拿到 (doc_id, score, meta)，
        # 没有正文就只能按融合分原样返回，等于没重排。上层取用时把它 pop 掉。
        meta["content"] = content or ""
        fused = vec_weight * float(vec_sim or 0) + kw_weight * _kw_score(query, content)
        out.append((doc_id, round(fused, 4), meta))
    out.sort(key=lambda x: -x[1])
    cands = out[: max(top_k * 2, top_k)]
    try:
        from .rerank import rerank_rows
        return rerank_rows(query, cands, top_k)
    except Exception as exc:                # noqa: BLE001 重排不能拖垮检索
        logger.warning("重排不可用，按融合分返回：%s", exc)
        return cands[:top_k]


class PgHybridRetriever(HybridRetriever):
    """生产版：PG + pgvector 向量召回 + 关键词召回 + 融合重排。

    psycopg 懒加载：未装 PG / 未装驱动时构造即抛错，由调用方回退 MockHybridRetriever。
    """

    def __init__(self, dsn: str, table: str = "knowledge_chunks"):
        from sca.settings import settings
        self.dsn = dsn or settings.pg_dsn
        self.table = table
        try:
            import psycopg
        except ImportError as exc:  # 未装驱动 -> 明确报错，便于上层回退
            raise RuntimeError("psycopg 未安装，无法使用 PG 混合检索") from exc
        self._psycopg = psycopg

    def add(self, chunks: List[Chunk]) -> None:
        """真实写入（含 embedding）：种子/增量入库统一入口。"""
        from sca.settings import settings

        vecs = embed_texts([c.content for c in chunks])
        with self._psycopg.connect(self.dsn) as conn:
            for c, vec in zip(chunks, vecs):
                if not vec:
                    continue  # 向量生成失败的条目不入库（保证库内向量完整）
                conn.execute(
                    "INSERT INTO knowledge_chunks(doc_id, content, embedding, metadata, content_tsv) "
                    "VALUES (%s,%s,%s,%s, to_tsvector('simple',%s)) "
                    "ON CONFLICT (doc_id) DO UPDATE SET "
                    "content=EXCLUDED.content, embedding=EXCLUDED.embedding, "
                    "metadata=EXCLUDED.metadata, content_tsv=EXCLUDED.content_tsv",
                    (c.doc_id, c.content, _vec_literal(vec),
                     json.dumps(c.metadata, ensure_ascii=False), c.content))
            conn.commit()

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]:
        hits = pg_vector_search(query, top_k=top_k)
        out: List[Tuple[Chunk, float]] = []
        for doc_id, score, meta in hits:
            meta = dict(meta or {})
            # 正文是内部为重排塞进 meta 的，不该出现在对外的 metadata 里
            content = str(meta.pop("content", "") or "")
            out.append((Chunk(doc_id, content, meta), score))
        return out


def build_retriever(use_pg: bool = False, dsn: str = "") -> HybridRetriever:
    """工厂：有 PG 走 PgHybridRetriever，否则 MockHybridRetriever（离线可跑）。"""
    if use_pg:
        try:
            return PgHybridRetriever(dsn)
        except RuntimeError as exc:
            logger.warning("[rag] PG 检索不可用，回退 Mock：%s", exc)
    return MockHybridRetriever()


# 内置知识库样本（SOP 片段），供 demo / 检索验证
SAMPLE_CHUNKS: List[Chunk] = [
    Chunk("SOP-P2-REROUTE", "拥堵/施工/偏航时自动重规划路径，保留计划版本并通知司机。",
          {"level": "P2", "type": "sop"}),
    Chunk("SOP-P0-COLD", "冷链失效立即挂起审批，启动备用冷源并通知收货方。",
          {"level": "P0", "type": "sop"}),
    Chunk("SOP-P0-DG", "危险品泄漏触发应急审批，隔离现场并上报监管。",
          {"level": "P0", "type": "sop"}),
    Chunk("SOP-P1-WX", "暴雪/台风预警前重规划并确认人工值守。",
          {"level": "P1", "type": "sop"}),
]
