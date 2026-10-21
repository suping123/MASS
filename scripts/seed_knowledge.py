"""知识库种子脚本：把全部 SOP（内置 + 自定义）写入 PG knowledge_chunks（FR-10）。

运行：python scripts/seed_knowledge.py
- 有 SCA_DASHSCOPE_API_KEY：调用 text-embedding-v3 生成真实 embedding 入库（1024 维）
- 无 Key：打印提示退出（向量召回不可用），不写入残缺数据
- 幂等：按 doc_id UPSERT，SOP 更新后重跑即可刷新向量
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sca.settings import settings  # noqa: E402


def main() -> None:
    try:
        import psycopg
    except ImportError:
        print("psycopg 未安装，跳过种子。"); return
    if not settings.use_pg or not settings.pg_dsn:
        print("SCA_USE_PG=false（未启用 PG），跳过种子。"); return
    if not settings.dashscope_api_key:
        print("未配置 SCA_DASHSCOPE_API_KEY：无法生成真实向量，跳过种子"
              "（配置后重跑本脚本即可）。"); return

    from sca.knowledge_base import build_chunks
    from sca.rag.hybrid import PgHybridRetriever

    chunks = build_chunks()
    print(f"待入库 {len(chunks)} 条 SOP 知识块（embedding 模型 {settings.embedding_model}）...")
    retriever = PgHybridRetriever(settings.pg_dsn)

    # 先清掉已删除的 SOP（如知识库删除操作后残留），再 UPSERT 当前全集
    keep_ids = [c.doc_id for c in chunks]
    with psycopg.connect(settings.pg_dsn) as conn:
        conn.execute("DELETE FROM knowledge_chunks WHERE doc_id != ALL(%s)", (keep_ids,))
        cur = conn.execute("SELECT count(*) FROM knowledge_chunks").fetchone()
        conn.commit()
    print(f"清理完成（库内现存 {cur[0]} 条）")

    retriever.add(chunks)

    with psycopg.connect(settings.pg_dsn) as conn:
        n, with_vec = conn.execute(
            "SELECT count(*), count(embedding) FROM knowledge_chunks").fetchone()
    print(f"种子完成：{n} 条知识块，其中 {with_vec} 条含真实向量。")
    if n and with_vec < n:
        print("警告：部分条目向量生成失败（见日志），可重跑本脚本补齐。")


if __name__ == "__main__":
    main()
