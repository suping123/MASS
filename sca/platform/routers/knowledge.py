"""调度平台的知识库接口：查状态、检索、增删改、重建。

知识库是调度侧的核心能力，权限沿用 `dispatch:plan`（只有调度员能改 SOP）。
每次增删改都会同步 PG 的向量块，避免"页面改了、检索还是旧的"。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import kb
from ..security import require_perm

router = APIRouter(prefix="/api/dispatch/knowledge", tags=["knowledge"])


class SearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=20)
    code: Optional[str] = Field(default=None, description="异常码，命中则直接锁定对应 SOP")


class SopReq(BaseModel):
    sop_id: Optional[str] = None
    title: str = Field(min_length=2, max_length=200)
    level: str = Field(default="P2", pattern="^(P0|P1|P2|P3)$")
    category: Optional[str] = None
    steps: List[str] = Field(min_length=1)
    owner: Optional[str] = None
    tools: List[str] = Field(default_factory=list)
    notify: Optional[str] = None


@router.get("/stats")
def stats(user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))) -> Dict[str, Any]:
    """知识库状态：块数、向量模型是否与库内一致、最近几次构建记录。"""
    return kb.stats()


@router.get("/history")
def history(limit: int = Query(default=20, ge=1, le=100),
            user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))
            ) -> List[Dict[str, Any]]:
    return kb.history(limit)


@router.post("/search")
def search(req: SearchReq,
           user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))
           ) -> Dict[str, Any]:
    """检索 SOP。优先走 PG 向量通道，PG 不可用时自动回退关键词检索并如实标注。"""
    from sca.knowledge_base import search_sop

    hits = search_sop(req.query, top_k=req.top_k, code=req.code or "")
    channel = hits[0].get("channel") if hits else None
    return {"hits": hits, "channel": channel,
            "note": "channel=pg-vector 表示走了 PG 向量检索；kw-fallback 表示回退到内存关键词"}


@router.get("/sops")
def list_sops(user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))) -> Dict[str, Any]:
    from sca.knowledge_base import list_sops as _list
    from sca.knowledge_base import summary

    return {"summary": summary(), "items": _list()}


@router.post("/sops")
def upsert_sop(req: SopReq,
               user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))
               ) -> Dict[str, Any]:
    """新增或修改 SOP。落 JSON 后立即同步 PG 向量块。"""
    from sca.knowledge_base import upsert_sop as _upsert

    payload = req.model_dump()
    try:
        saved = _upsert(payload)
    except Exception as exc:  # noqa: BLE001 校验失败要原样回给调用方
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from None

    sop_id = (saved or {}).get("sop_id") or payload.get("sop_id")
    pg = kb.upsert_one(sop_id) if sop_id else {"ok": False, "error": "未取得 sop_id"}
    return {"saved": saved, "pg": pg,
            "note": "PG 同步失败时知识库仍可用（回退关键词检索），但语义召回会退化为旧版本"}


@router.delete("/sops/{sop_id}")
def delete_sop(sop_id: str,
               user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))
               ) -> Dict[str, Any]:
    from sca.knowledge_base import delete_sop as _delete

    res = _delete({"sop_id": sop_id})
    pg = kb.delete_one(sop_id)
    return {"deleted": res, "pg": pg}


@router.post("/rebuild")
def rebuild(user: Dict[str, Any] = Depends(require_perm("dispatch:plan"))
            ) -> Dict[str, Any]:
    """全量重建：重新建表 + 重新生成全部向量 + 清理已删除的条目。

    换 embedding 模型之后必须跑一次，否则新旧向量混算，分数不可比。
    """
    schema = kb.ensure_schema()
    if not schema.get("ok"):
        return {"ok": False, "stage": "schema", **schema}
    return {"ok": True, "stage": "sync", "schema": schema, "result": kb.sync(full=True)}
