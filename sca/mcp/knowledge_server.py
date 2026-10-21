"""MCP 工具层（FR-11 / FR-10）：知识库 / SOP 检索 Server。

基于混合检索（向量 + 关键词 + 融合重排，见 sca.rag）。离线用内存 MockHybridRetriever；
接 PG + pgvector 时由 build_retriever(use_pg=True) 自动切换。
"""
from __future__ import annotations

import logging

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.knowledge")
mcp = FastMCP("knowledge-mcp") if HAVE_MCP else _StubMCP()

# 离线检索器（生产由 settings.use_pg 切换 PG 版）
from sca.rag.hybrid import MockHybridRetriever, SAMPLE_CHUNKS, build_retriever

_retr = build_retriever(use_pg=False)
for _c in SAMPLE_CHUNKS:
    _retr.add([_c])


@mcp.tool()
async def search_knowledge(query: str, top_k: int = 3) -> list:
    """SOP / 知识库检索（FR-10）：返回命中的 SOP 片段与融合得分。"""
    return [{"doc_id": c.doc_id, "content": c.content, "score": s,
             "metadata": c.metadata} for c, s in _retr.search(query, top_k)]


@mcp.tool()
async def list_sops() -> list:
    """列出知识库内全部 SOP 标识。"""
    return [c.doc_id for c in SAMPLE_CHUNKS]


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
