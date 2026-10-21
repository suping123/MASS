"""PG 迁移脚本：创建向量库与业务表（需求 §11，PG + pgvector 一栈到底）。

运行：python scripts/migrate_pg.py   （需 SCA_PG_DSN 或 .env，且已安装 psycopg）
仅在 use_pg=True 的生产/联调环境执行；原型骨架默认可不跑（内存检查点）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sca.settings import settings  # noqa: E402

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGSERIAL PRIMARY KEY,
    doc_id      TEXT UNIQUE,
    content     TEXT,
    embedding   vector(1024),
    metadata    JSONB,
    content_tsv  TSVECTOR,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_kc_vec ON knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS cases (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT,
    level       TEXT,
    sop_id      TEXT,
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS plans (
    id          BIGSERIAL PRIMARY KEY,
    order_id    TEXT,
    version     INT,
    plan_json   JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS approvals (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT,
    level       TEXT,
    decision    JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);
"""


def main() -> None:
    try:
        import psycopg
    except ImportError:
        print("psycopg 未安装，跳过迁移（原型内存模式无需 PG）。"); return
    dsn = settings.pg_dsn
    with psycopg.connect(dsn) as conn:
        conn.execute(DDL)
        conn.commit()
    print(f"PG 迁移完成：{dsn}")


if __name__ == "__main__":
    main()
