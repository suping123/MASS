-- =====================================================================
-- 异常处置知识库（PostgreSQL + pgvector）
--
-- 为什么知识库用 PG 而不是 MySQL：
--   * 需要向量近邻检索（`<=>` 余弦距离）与 jsonb 元数据过滤，MySQL 侧没有等价物；
--   * 知识条目是"只追加 + 全量可重建"的形态，与业务订单的强事务不同，
--     分开存不会牵动订单库的表结构与锁竞争。
--
-- 与现有代码的契约（不可改动字段名）：
--   sca/rag/hybrid.py::PgHybridRetriever.add  写 doc_id/content/embedding/metadata/content_tsv
--   sca/rag/hybrid.py::pg_vector_search       读 同样四个字段 + 1-(embedding <=> q)
--
-- embedding 列刻意**不锁维度**（`vector` 而非 `vector(1024)`）：
--   换 embedding 模型时维度会变，锁死维度会导致"新模型写不进去、旧库也读不出"。
--   维度与模型记录在 kb_meta，检索前校验，不一致时明确报错而不是静默降级。
--   数据量在千级以内，顺序扫描足够快，故不建 HNSW 索引（无索引也不影响正确性）。
--
-- 执行：
--   "C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\pg_install\pgsql\bin\psql.exe" ^
--       -U sca -d sca -f sql/knowledge_pg.sql
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- 检索块：一条 SOP 一个块（现有 build_chunks 的粒度），doc_id 即 sop_id
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  doc_id      varchar(64) PRIMARY KEY,
  content     text        NOT NULL,
  embedding   vector,
  metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  content_tsv tsvector,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 兼容更早版本的表：本表在 2026-10 之前就已存在（旧版只有前五个字段）。
-- 只靠 CREATE TABLE IF NOT EXISTS 会跳过已存在的表，随后建索引就会报
-- 「字段 updated_at 不存在」。故用 ADD COLUMN IF NOT EXISTS 做向前兼容。
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS metadata   jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector;
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS updated_at  timestamptz NOT NULL DEFAULT now();

COMMENT ON TABLE knowledge_chunks IS '异常处置知识库检索块（SOP/案例），由 rebuild 全量重建';

-- 元数据：当前库用的是哪个 embedding 模型、多少维、何时构建
CREATE TABLE IF NOT EXISTS kb_meta (
  key   varchar(64) PRIMARY KEY,
  value jsonb       NOT NULL
);

-- 构建/更新审计：每次 rebuild 或增量同步留一条，便于回答"知识库什么时候更新的"
CREATE TABLE IF NOT EXISTS kb_build_log (
  id         bigserial    PRIMARY KEY,
  action     varchar(24)  NOT NULL,
  docs       integer      NOT NULL DEFAULT 0,
  chunks     integer      NOT NULL DEFAULT 0,
  failed     integer      NOT NULL DEFAULT 0,
  model      varchar(64),
  dim        integer,
  detail     jsonb        NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kc_updated ON knowledge_chunks (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_log_time ON kb_build_log (created_at DESC);
