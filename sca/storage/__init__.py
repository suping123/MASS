"""检查点 / 存储层（需求 §11）。

优先使用 AsyncPostgresSaver（PG + pgvector 一栈到底，支持中断恢复与计划版本可追溯，
跨进程/重启可恢复审批现场）；未装 PG 或 use_pg=false 时自动回退 MemorySaver，
保证骨架在无数据库环境下也能跑通。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any, Dict, List, Optional

from sca.settings import settings

logger = logging.getLogger("sca.storage")

# langgraph 校验 checkpointer 必须是 BaseCheckpointSaver 实例（ensure_valid_checkpointer）
try:
    from langgraph.checkpoint.base import BaseCheckpointSaver as _BaseSaver
except Exception:  # 未装 langgraph（纯垫片模式）-> 普通基类
    class _BaseSaver:  # type: ignore[no-redef]
        pass


class _LazyAsyncPgSaver(_BaseSaver):
    """惰性 AsyncPostgresSaver 代理：首次真正使用时（有事件循环）才建连接池与建表。

    继承 BaseCheckpointSaver 以通过 langgraph 的 ensure_valid_checkpointer 校验；
    只实现异步接口（aput/aput_writes/aget_tuple/alist/adelete_thread），本系统状态机
    全部走 ainvoke/异步路径。
    """

    def __init__(self, dsn: str):
        try:
            super().__init__()
        except Exception:  # noqa: 垫片基类无 __init__ 签名约束
            pass
        self.dsn = dsn
        self._saver: Any = None
        self._pool: Any = None

    async def _ensure(self):
        if self._saver is None:
            try:
                from psycopg_pool import AsyncConnectionPool
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            except ImportError as exc:
                # 典型误因：用系统 Python / 其它 venv 起了服务（缺 psycopg-pool 或
                # langgraph-checkpoint-postgres）。此时连接池尚未建立，可安全降级。
                raise RuntimeError(
                    f"PG 检查点依赖缺失（{exc}）。当前解释器：{sys.executable}。"
                    "请用项目 .venv 运行（双击 start_server.bat / start_all.bat），"
                    "或在 .env 设置 SCA_USE_PG=false 回退 MemorySaver。"
                ) from exc

            # 与官方 from_conn_string 相同的连接参数（autocommit 必须：setup 的
            # CREATE INDEX CONCURRENTLY 不能在事务块内执行）；min_size=1 避免
            # 后台扩池任务在事件循环关闭后重试导致进程无法退出
            self._pool = AsyncConnectionPool(
                self.dsn, open=False, timeout=15, min_size=1, max_size=10,
                kwargs={"autocommit": True, "prepare_threshold": 0})
            await self._pool.open()
            self._saver = AsyncPostgresSaver(self._pool)
            await self._saver.setup()  # 建检查点表（幂等）
            logger.info("[storage] AsyncPostgresSaver 就绪：%s", self._self_masked())
        return self._saver

    def _self_masked(self) -> str:
        return self.dsn.split("@")[-1] if "@" in self.dsn else self.dsn

    async def aput(self, *a, **k):
        return await (await self._ensure()).aput(*a, **k)

    async def aput_writes(self, *a, **k):
        return await (await self._ensure()).aput_writes(*a, **k)

    async def aget_tuple(self, *a, **k):
        return await (await self._ensure()).aget_tuple(*a, **k)

    async def alist(self, *a, **k):
        async for item in (await self._ensure()).alist(*a, **k):
            yield item

    async def adelete_thread(self, *a, **k):
        return await (await self._ensure()).adelete_thread(*a, **k)

    async def aexecute(self, sql: str, params: tuple = ()) -> int:
        """在检查点连接池上执行一条 SQL，返回受影响行数。

        用途：「重新审批」需要把 LangGraph thread 的检查点回退到 interrupt 现场
        （删掉之后的检查点 / 过期 writes / 过期 blob），官方 saver 没有暴露这种
        "部分回退" 接口，只能对同一批表做精确 DELETE。连接池 autocommit，逐条提交。
        """
        await self._ensure()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount

    async def ensure(self):
        """公开的初始化入口：建连接池 + setup（幂等）。供记忆存储等非检查点调用方使用。"""
        return await self._ensure()

    def pg_pool(self):
        """底层连接池（ensure() 之后可用）。供需要显式事务的调用方使用——
        如记忆 fold：UPDATE 旧版 superseded + INSERT 新版 + INSERT 审计必须同事务。"""
        if self._pool is None:
            raise RuntimeError("请先 await ensure() 再取连接池")
        return self._pool

    async def afetch(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """查询多行，返回 dict 列表（dict_row）。"""
        from psycopg.rows import dict_row

        await self._ensure()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return [dict(r) for r in await cur.fetchall()]

    async def afetch_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """查询单行，返回 dict 或 None。"""
        from psycopg.rows import dict_row

        await self._ensure()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    async def aclose(self):
        """关闭底层连接池（一次性使用者如「清除旧检查点」调用，避免池子泄漏）。"""
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as exc:  # noqa: 关闭失败不影响主流程
                logger.warning("[storage] 连接池关闭失败：%s", exc)
            self._pool = None
            self._saver = None

    # 同步接口：本系统不走（避免在事件循环里做阻塞 IO）
    def get_tuple(self, *a, **k):
        raise RuntimeError("请使用异步接口（本系统状态机全部走 ainvoke）")


def _pg_deps_missing() -> Optional[str]:
    """探测 PG 检查点所需依赖是否可导入；返回缺失的模块名，全部就绪返回 None。

    只探测不导入（find_spec 无副作用），供启动期决定是否回退 MemorySaver。
    注意：这只覆盖「依赖没装」——PG 连不上仍然在首次使用时暴露（运维可见）。
    """
    for mod in ("psycopg_pool", "langgraph.checkpoint.postgres.aio"):
        try:
            if importlib.util.find_spec(mod) is None:
                return mod
        except Exception:  # noqa: 探测本身失败（父包损坏等）按缺失处理
            return mod
    return None


def pg_checkpointer() -> Optional[_LazyAsyncPgSaver]:
    """构造 PG 检查点代理（不连库，不抛错）；连接失败在首次使用时才暴露。

    依赖缺失（用错了解释器 / 没装 psycopg-pool）时按 docstring 承诺回退
    MemorySaver 并打醒目的 WARNING——否则错误会推迟到第一次图调用才以
    「No module named 'psycopg_pool'」的形式炸在请求里。
    """
    if settings.use_pg and settings.pg_dsn:
        missing = _pg_deps_missing()
        if missing:
            logger.warning(
                "[storage] 缺少 PG 检查点依赖 %s（当前解释器 %s），回退 MemorySaver；"
                "跨进程/重启的中断恢复与重处理检查点回退将不可用——请用项目 .venv "
                "运行（start_server.bat）", missing, sys.executable)
            return None
        try:
            saver = _LazyAsyncPgSaver(settings.pg_dsn)
            return saver
        except Exception as exc:  # noqa
            logger.warning("[storage] PG 检查点构造失败，回退 MemorySaver：%s", exc)
    return None


def get_checkpointer():
    cp = pg_checkpointer()
    if cp is not None:
        return cp
    from sca._compat import MemorySaver

    logger.info("[storage] 使用 MemorySaver（内存检查点）")
    return MemorySaver()


# 知识库向量表 DDL（PG + pgvector）；use_pg 时由迁移脚本执行
KNOWLEDGE_CHUNKS_DDL = """
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
"""
