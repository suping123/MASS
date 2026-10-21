"""混合检索（FR-10）：向量召回 + 关键词(tsvector/pg_trgm) + 重排。

离线（无 PG）用内存版 MockHybridRetriever 跑通检索逻辑；生产接 PG + pgvector 时
切换到 PgHybridRetriever（psycopg 懒加载，未装不报错）。
"""
