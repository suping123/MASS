"""异常处置知识库（PostgreSQL + pgvector）的构建、更新与状态。

分工：
  * 检索复用 `sca.rag.hybrid.pg_vector_search`（向量 ANN + 中文 2-gram 融合），
    不另写一套，避免两处打分口径漂移；
  * 本模块只负责**写入侧**：建表、全量重建、单条增量、一致性校验、审计留痕。

三条约束：
  1) **embedding 模型必须与库内一致**。换模型必须全量重建，否则新旧向量混在
     一起算余弦距离，分数没有意义（而且不会报错，只会悄悄变差）。
  2) **向量生成失败不入库**。宁可少一条，也不写 NULL 让它在检索时永远排最后
     却占着 doc_id（看起来"有这条知识"，实际永远召回不到）。
  3) **重建是可见的**。每次 rebuild / upsert 都写 kb_build_log，能回答
     "这条 SOP 是什么时候进库的、用的哪个模型"。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sca.settings import settings

SQL_FILE = Path(__file__).resolve().parents[2] / "sql" / "knowledge_pg.sql"


# ------------------------------------------------------------------ 连接
def _dsn() -> str:
    return (settings.pg_dsn or "").strip()


def _connect(timeout: int = 5):
    import psycopg

    return psycopg.connect(_dsn(), connect_timeout=timeout)


_AVAIL: Dict[str, Any] = {"at": 0.0, "ok": False, "why": "尚未探测"}
_AVAIL_TTL = 30.0


def available(force: bool = False) -> Tuple[bool, str]:
    """返回 (是否可用, 原因)。原因要能直接给人看，不能只说 False。

    结果缓存 30 秒：知识库状态会被页面轮询，而 PG 挂掉时每次探测要等 5 秒连接超时，
    不缓存会让调度台每个刷新都卡 5 秒。
    """
    if not settings.use_pg:
        return False, "SCA_USE_PG=false，PG 未启用"
    if not _dsn():
        return False, "SCA_PG_DSN 未配置"
    now = time.time()
    if not force and now - float(_AVAIL["at"]) < _AVAIL_TTL:
        return bool(_AVAIL["ok"]), str(_AVAIL["why"])
    try:
        with _connect(timeout=3) as conn:
            conn.execute("SELECT 1 FROM knowledge_chunks LIMIT 1")
        _AVAIL.update({"at": now, "ok": True, "why": ""})
        return True, ""
    except Exception as exc:  # noqa: BLE001
        why = f"{type(exc).__name__}: {str(exc)[:160]}"
        _AVAIL.update({"at": now, "ok": False, "why": why})
        return False, why


# ------------------------------------------------------------------ 建表
def ensure_schema() -> Dict[str, Any]:
    """执行 sql/knowledge_pg.sql（幂等）。缺 pgvector 扩展时给出明确提示。"""
    if not SQL_FILE.exists():
        return {"ok": False, "error": f"找不到建表脚本 {SQL_FILE}"}
    sql = SQL_FILE.read_text(encoding="utf-8")
    stmts = [s.strip() for s in sql.split(";") if s.strip()
             and not all(ln.strip().startswith("--") or not ln.strip()
                         for ln in s.splitlines())]
    try:
        with _connect(timeout=10) as conn:
            for stmt in stmts:
                conn.execute(stmt)
        return {"ok": True, "statements": len(stmts)}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        hint = ""
        if "vector" in msg.lower() and ("does not exist" in msg or "extension" in msg.lower()):
            hint = ("pgvector 扩展不可用：确认 pg_install/pgsql/lib/vector.dll 存在，"
                    "并以有权限的账号执行 CREATE EXTENSION vector")
        return {"ok": False, "error": msg[:300], "hint": hint}


# ------------------------------------------------------------------ 重建
def sync(full: bool = True) -> Dict[str, Any]:
    """从 `sca.knowledge_base` 读全部 SOP，生成向量并写入 PG。

    full=True 时，库内不在本次集合中的 doc_id 会被删除——删 SOP 后必须调用，
    否则"已删除的知识"还能被检索到，比召回不到更危险。
    """
    t0 = time.time()
    ok, why = available() if not full else (True, "")
    if not ok:
        return {"ok": False, "error": why}

    from sca.knowledge_base import build_chunks
    from sca.rag.hybrid import embed_texts

    chunks = build_chunks()
    if not chunks:
        return {"ok": False, "error": "知识库为空，未生成任何检索块"}

    vecs = embed_texts([c.content for c in chunks])
    doc_ids = [c.doc_id for c in chunks]
    pairs = [(c, v) for c, v in zip(chunks, vecs) if v]
    failed = len(chunks) - len(pairs)
    if not pairs:
        return {"ok": False,
                "error": "全部向量生成失败，未写入任何数据（检查 SCA_DASHSCOPE_API_KEY 与网络）",
                "failed": failed}

    dim = len(pairs[0][1])
    bad_dim = [c.doc_id for c, v in pairs if len(v) != dim]
    if bad_dim:
        return {"ok": False, "error": f"向量维度不一致：{bad_dim[:5]}"}

    try:
        with _connect(timeout=15) as conn:
            for c, v in pairs:
                conn.execute(
                    "INSERT INTO knowledge_chunks(doc_id, content, embedding, metadata, content_tsv) "
                    "VALUES (%s,%s,%s::vector,%s, to_tsvector('simple',%s)) "
                    "ON CONFLICT (doc_id) DO UPDATE SET "
                    "content=EXCLUDED.content, embedding=EXCLUDED.embedding, "
                    "metadata=EXCLUDED.metadata, content_tsv=EXCLUDED.content_tsv, "
                    "updated_at=now()",
                    (c.doc_id, c.content, _vec(v),
                     json.dumps(c.metadata, ensure_ascii=False), c.content))
            removed = 0
            if full:
                cur = conn.execute(
                    "DELETE FROM knowledge_chunks WHERE NOT (doc_id = ANY(%s))",
                    (doc_ids,))
                removed = cur.rowcount or 0
            conn.execute(
                "INSERT INTO kb_meta(key, value) VALUES ('embedding', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps({"model": settings.embedding_model, "dim": dim,
                             "docs": len(pairs), "built_at": _now()},
                            ensure_ascii=False),))
            conn.execute(
                "INSERT INTO kb_build_log(action, docs, chunks, failed, model, dim, detail) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("rebuild" if full else "sync", len(pairs), len(pairs), failed,
                 settings.embedding_model, dim,
                 json.dumps({"removed": removed}, ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    return {"ok": True, "docs": len(pairs), "chunks": len(pairs), "failed": failed,
            "removed": removed if full else 0, "model": settings.embedding_model,
            "dim": dim, "elapsed_ms": int((time.time() - t0) * 1000)}


def upsert_one(sop_id: str) -> Dict[str, Any]:
    """单条增量：改了某份 SOP 之后调用，只重建这一条，不惊动全库。"""
    from sca.knowledge_base import build_chunks
    from sca.rag.hybrid import embed_texts

    chunk = next((c for c in build_chunks() if c.doc_id == sop_id), None)
    if chunk is None:
        return delete_one(sop_id)      # 已被删除 -> 走删除路径，避免留下孤儿
    vec = embed_texts([chunk.content])[0]
    if not vec:
        return {"ok": False, "error": "向量生成失败（未配 Key 或 API 不可用）"}
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO knowledge_chunks(doc_id, content, embedding, metadata, content_tsv) "
                "VALUES (%s,%s,%s::vector,%s, to_tsvector('simple',%s)) "
                "ON CONFLICT (doc_id) DO UPDATE SET "
                "content=EXCLUDED.content, embedding=EXCLUDED.embedding, "
                "metadata=EXCLUDED.metadata, content_tsv=EXCLUDED.content_tsv, updated_at=now()",
                (chunk.doc_id, chunk.content, _vec(vec),
                 json.dumps(chunk.metadata, ensure_ascii=False), chunk.content))
            conn.execute(
                "INSERT INTO kb_build_log(action, docs, chunks, model, dim, detail) "
                "VALUES ('upsert',1,1,%s,%s,%s)",
                (settings.embedding_model, len(vec),
                 json.dumps({"sop_id": sop_id}, ensure_ascii=False)))
        return {"ok": True, "sop_id": sop_id, "dim": len(vec)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def delete_one(sop_id: str) -> Dict[str, Any]:
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=%s", (sop_id,))
            n = cur.rowcount or 0
            conn.execute(
                "INSERT INTO kb_build_log(action, docs, chunks, detail) VALUES ('delete',0,%s,%s)",
                (n, json.dumps({"sop_id": sop_id}, ensure_ascii=False)))
        return {"ok": True, "sop_id": sop_id, "deleted": n}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


# ------------------------------------------------------------------ 状态
def stats() -> Dict[str, Any]:
    ok, why = available()
    base: Dict[str, Any] = {"available": ok}
    if not ok:
        base["reason"] = why
        return base
    try:
        with _connect() as conn:
            n = conn.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0]
            missing_vec = conn.execute(
                "SELECT count(*) FROM knowledge_chunks WHERE embedding IS NULL").fetchone()[0]
            meta = conn.execute(
                "SELECT value FROM kb_meta WHERE key='embedding'").fetchone()
            logs = conn.execute(
                "SELECT action, docs, chunks, failed, model, dim, created_at "
                "FROM kb_build_log ORDER BY id DESC LIMIT 5").fetchall()
        info = meta[0] if meta else {}
        dim_in_db = int(info.get("dim") or 0) if isinstance(info, dict) else 0
        consistent = (not dim_in_db) or (info.get("model") == settings.embedding_model)
        base.update({
            "chunks": int(n), "missing_vec": int(missing_vec or 0),
            "model_in_db": info.get("model"), "dim_in_db": dim_in_db,
            "model_now": settings.embedding_model,
            "built_at": info.get("built_at"),
            "consistent": consistent,
            "recent_builds": [{"action": r[0], "docs": r[1], "chunks": r[2],
                               "failed": r[3], "model": r[4], "dim": r[5],
                               "at": r[6].isoformat(timespec="seconds")} for r in logs],
        })
        if not consistent:
            base["warning"] = (f"库内向量由 {info.get('model')} 生成，当前配置是 "
                               f"{settings.embedding_model}；两者不可比，请执行 rebuild")
        return base
    except Exception as exc:  # noqa: BLE001
        base.update({"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})
        return base


def history(limit: int = 20) -> List[Dict[str, Any]]:
    ok, _ = available()
    if not ok:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT action, docs, chunks, failed, model, dim, detail, created_at "
            "FROM kb_build_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{"action": r[0], "docs": r[1], "chunks": r[2], "failed": r[3],
             "model": r[4], "dim": r[5], "detail": r[6],
             "at": r[7].isoformat(timespec="seconds")} for r in rows]


# ------------------------------------------------------------------ 工具
def _vec(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
