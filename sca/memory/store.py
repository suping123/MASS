"""记忆存储：PG 真相层 + Redis 热层 + WAL 异步写入。

写入路径（commit）：
  Guard/Resolver 裁定后的 op -> ① 值脱敏（mask_text，与 sessions.py 同一硬约束）
  -> ② Redis 写穿透（HSET mem:{user}:{key}，请求内完成，保证会话内读个人写）
  -> ③ XADD mem:wal（WAL，Redis AOF 持久化，不丢）
  -> ④ 仅 pg_direct 模式：立即 fold 进 PG
  ②成功但③失败：回滚 ② 并就地 fold 进 PG（不丢、不双重生效）。

fold worker（fold_once / fold_loop）：
  XREADGROUP memfold -> 逐条显式事务落 PG（同键旧版 superseded + 新版
  version+1 + memory_ops 审计；op_id 主键 + 事务内查重保证幂等）-> XACK。
  失败不 XACK，下轮重试。

模式与降级：
  redis 模式   = use_redis 且 Redis 可达（写穿透 + WAL，PG 异步追平）
  pg_direct   = Redis 不可达 / use_redis=false（跳过 ②③，④立即执行）
  两者正确性等价，差别只在延迟与"PG 短暂未追平"的窗口。降级状态带
  重探间隔（30s），Redis 恢复后自动回到 redis 模式。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sca.settings import settings
from sca.storage import _LazyAsyncPgSaver

from .models import (OP_DELETE, OP_ERASE, OP_UPSERT, STATUS_DELETED,
                     STATUS_SUPERSEDED, STATUS_VALID, KEY_REGISTRY, MemoryItem,
                     MemoryOp)
from .resolve import CONTESTED, KEEP, SKIP

logger = logging.getLogger("sca.memory")

_DDL: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory_items (
        id           BIGSERIAL PRIMARY KEY,
        user_id      TEXT NOT NULL,
        key          TEXT NOT NULL,
        value        JSONB NOT NULL,
        type         TEXT NOT NULL DEFAULT 'preference',
        version      INT NOT NULL DEFAULT 1,
        status       TEXT NOT NULL DEFAULT 'valid',
        valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
        valid_to     TIMESTAMPTZ,
        recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        source       TEXT NOT NULL DEFAULT 'explicit',
        confidence   REAL NOT NULL DEFAULT 1.0,
        evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,
        pii_level    TEXT NOT NULL DEFAULT 'internal',
        content_hash TEXT NOT NULL DEFAULT '',
        session_id   TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_valid ON memory_items (user_id, key) WHERE status = 'valid'",
    "CREATE INDEX IF NOT EXISTS ix_mem_user_status ON memory_items (user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS memory_ops (
        op_id      TEXT PRIMARY KEY,
        ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
        user_id    TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        action     TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      JSONB,
        type       TEXT,
        source     TEXT,
        confidence REAL,
        actor      TEXT NOT NULL DEFAULT 'system',
        reason     TEXT NOT NULL DEFAULT '',
        decision   TEXT NOT NULL DEFAULT '',
        version    INT,
        evidence   JSONB NOT NULL DEFAULT '[]'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_memops_user_ts ON memory_ops (user_id, ts DESC)",
)

_MODE_RECHECK_DEGRADED = 30.0   # 降级状态下的重探间隔（秒）
_MODE_RECHECK_UP = 300.0        # 正常状态下的重探间隔（秒）


class MemoryStore:
    """记忆存储单例。所有方法幂等；Redis 不可达自动降级，不抛到调用方。"""

    def __init__(self) -> None:
        self._pg: Optional[_LazyAsyncPgSaver] = None
        self._redis: Any = None
        self._mode: Optional[str] = None
        self._mode_checked_at: float = 0.0
        self._ddl_done = False
        self._degraded_warned = False
        self.stats: Dict[str, int] = {"commit_ok": 0, "commit_fail": 0,
                                      "folded": 0, "fold_fail": 0, "degrade": 0,
                                      "contested": 0, "skipped": 0}

    # ------------------------------------------------------------------ 基础
    def _pg_saver(self) -> _LazyAsyncPgSaver:
        if self._pg is None:
            self._pg = _LazyAsyncPgSaver(settings.pg_dsn)
        return self._pg

    async def _ensure_pg(self) -> _LazyAsyncPgSaver:
        pg = self._pg_saver()
        await pg.ensure()
        if not self._ddl_done:
            for stmt in _DDL:
                await pg.aexecute(stmt)
            self._ddl_done = True
            logger.info("[memory] PG 表就绪（memory_items / memory_ops）")
        return pg

    async def _ensure_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis.from_url(
                settings.redis_url, decode_responses=True,
                socket_timeout=2.0, socket_connect_timeout=2.0)
        return self._redis

    async def _probe_redis(self) -> bool:
        try:
            r = await self._ensure_redis()
            await r.ping()
            return True
        except Exception as exc:
            if not self._degraded_warned:
                logger.warning("[memory] Redis 不可达，降级 pg_direct（正确性不变，"
                               "延迟升一档）：%s", exc)
                self._degraded_warned = True
                self.stats["degrade"] += 1
            return False

    async def mode(self) -> str:
        """当前写入模式；带重探间隔，避免 Redis 挂掉时每次 commit 都等连接超时。"""
        if not settings.use_redis:
            return "pg_direct"
        import time as _t

        now = _t.time()
        ttl = _MODE_RECHECK_DEGRADED if self._mode == "pg_direct" else _MODE_RECHECK_UP
        if self._mode and now - self._mode_checked_at < ttl:
            return self._mode
        self._mode = "redis" if await self._probe_redis() else "pg_direct"
        self._mode_checked_at = now
        if self._mode == "redis":
            self._degraded_warned = False
            try:
                await self._ensure_wal_group()
            except Exception as exc:  # BUSYGROUP 等：组已存在，忽略
                if "BUSYGROUP" not in str(exc):
                    logger.warning("[memory] WAL 消费组准备失败：%s", exc)
        return self._mode

    @staticmethod
    def _user_hash(user_id: str) -> str:
        return f"mem:{user_id}"

    # ------------------------------------------------------------------ 写入
    async def commit(self, user_id: str,
                     resolved: List[Tuple[MemoryOp, str, str]],
                     existing: Dict[str, MemoryItem],
                     session_id: str = "") -> Dict[str, Any]:
        """执行裁定后的记忆变更。resolved 元素 = (op, decision, reason)。"""
        from sca.sessions import mask_text  # 唯一写盘路口同款脱敏硬约束

        mode = await self.mode()
        committed: List[Dict[str, Any]] = []
        for op, decision, reason in resolved:
            if decision == SKIP:
                self.stats["skipped"] += 1
                continue
            if decision == CONTESTED:
                self.stats["contested"] += 1
                logger.info("[memory] 提案待澄清：key=%s reason=%s", op.key, reason)
                continue
            # decision == KEEP
            if op.action == OP_UPSERT and isinstance(op.value, str):
                op.value = mask_text(op.value)
            op.user_id, op.session_id = user_id, session_id
            cur = existing.get(op.key)
            if mode == "redis":
                ok = await self._commit_redis(op, cur)
                if not ok:
                    # 写穿透成功但 WAL 失败（或反向）：回退热层，就地落 PG
                    await self._rollback_redis(op, cur)
                    ver = await self._fold_op(op)
                    committed.append({"key": op.key, "action": op.action,
                                      "version": ver, "path": "pg_direct(fallback)"})
                    continue
                committed.append({"key": op.key, "action": op.action,
                                  "version": (cur.version + 1) if cur else 1,
                                  "path": "redis+wal"})
            else:
                ver = await self._fold_op(op)
                committed.append({"key": op.key, "action": op.action,
                                  "version": ver, "path": "pg_direct"})
        self.stats["commit_ok"] += 1
        return {"mode": mode, "committed": committed,
                "user_id": user_id, "session_id": session_id}

    async def _commit_redis(self, op: MemoryOp, cur: Optional[MemoryItem]) -> bool:
        """Redis 写穿透 + WAL append。任一失败返回 False（调用方回退 PG）。"""
        try:
            r = await self._ensure_redis()
            hkey = self._user_hash(op.user_id)
            if op.action == OP_UPSERT:
                item = MemoryItem(
                    key=op.key, value=op.value, type=op.type,
                    version=(cur.version + 1) if cur else 1,
                    source=op.source, confidence=op.confidence,
                    evidence=list(op.evidence),
                    pii_level=KEY_REGISTRY[op.key].pii_level,
                    user_id=op.user_id, session_id=op.session_id)
                await r.hset(hkey, op.key, json.dumps(
                    _item_to_dict(item), ensure_ascii=False))
            else:
                # 删除在热层写墓碑而不是删键：热层是"含删除的当前态"权威视图。
                # 若直接 HDEL，"热层空 -> 从 PG 回填"的自愈路径会把擦除前的旧值
                # 当成本地状态返回（PG 还没追平 WAL 里的 erase）。墓碑占住键位，
                # recall 按 status=valid 过滤自然排除；真正的 Redis 丢数据
                # （整体为空）仍走 PG 回填自愈。
                tomb = MemoryItem(
                    key=op.key, value=None, type=op.type,
                    version=(cur.version + 1) if cur else 1,
                    status=STATUS_DELETED, valid_to=time.time(),
                    source=op.source, user_id=op.user_id,
                    session_id=op.session_id)
                await r.hset(hkey, op.key, json.dumps(
                    _item_to_dict(tomb), ensure_ascii=False))
            await r.xadd(settings.memory_wal_stream,
                         {"op": json.dumps(op.to_dict(), ensure_ascii=False)})
            return True
        except Exception as exc:
            logger.warning("[memory] Redis 写入失败，回退 PG：%s", exc)
            return False

    async def _rollback_redis(self, op: MemoryOp, cur: Optional[MemoryItem]) -> None:
        """WAL 失败后回退热层：恢复旧值（有则写回，无则删键——含墓碑）。"""
        try:
            r = await self._ensure_redis()
            hkey = self._user_hash(op.user_id)
            if cur is not None:
                await r.hset(hkey, op.key, json.dumps(
                    _item_to_dict(cur), ensure_ascii=False))
            else:
                await r.hdel(hkey, op.key)
        except Exception as exc:  # noqa 回滚失败不阻断（PG 为准）
            logger.warning("[memory] Redis 回滚失败（以 PG 为准）：%s", exc)

    # ------------------------------------------------------------------ fold
    async def _fold_op(self, op: MemoryOp) -> int:
        """把单条 op 折进 PG（显式事务；幂等）。返回生效后的版本号。"""
        pg = await self._ensure_pg()
        pool = pg.pg_pool()
        ts = datetime.fromtimestamp(op.ts, timezone.utc)
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur_db:
                    await cur_db.execute(
                        "SELECT 1 FROM memory_ops WHERE op_id = %s", (op.op_id,))
                    if await cur_db.fetchone():
                        return 0            # 已折过（WAL 重投），幂等跳过
                    await cur_db.execute(
                        "SELECT id, version FROM memory_items "
                        "WHERE user_id = %s AND key = %s AND status = %s FOR UPDATE",
                        (op.user_id, op.key, STATUS_VALID))
                    row = await cur_db.fetchone()
                    old_version = int(row[1]) if row else 0
                    if op.action in (OP_DELETE, OP_ERASE):
                        if row:
                            await cur_db.execute(
                                "UPDATE memory_items SET status = %s, valid_to = %s, "
                                "updated_at = %s WHERE id = %s",
                                (STATUS_DELETED, ts, ts, row[0]))
                        await self._insert_op(cur_db, op, old_version, reason=op.reason,
                                              decision=op.action)
                        return old_version
                    if row:
                        await cur_db.execute(
                            "UPDATE memory_items SET status = %s, valid_to = %s, "
                            "updated_at = %s WHERE id = %s",
                            (STATUS_SUPERSEDED, ts, ts, row[0]))
                    await cur_db.execute(
                        "INSERT INTO memory_items (user_id, key, value, type, version, "
                        "status, valid_from, valid_to, recorded_at, updated_at, source, "
                        "confidence, evidence, pii_level, content_hash, session_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (op.user_id, op.key, json.dumps(op.value, ensure_ascii=False),
                         op.type, old_version + 1, STATUS_VALID, ts, None, ts, ts,
                         op.source, op.confidence,
                         json.dumps(op.evidence, ensure_ascii=False),
                         KEY_REGISTRY[op.key].pii_level,
                         MemoryItem(key=op.key, value=op.value).content_hash,
                         op.session_id))
                    await self._insert_op(cur_db, op, old_version + 1, reason=op.reason,
                                          decision="applied")
                    return old_version + 1

    @staticmethod
    async def _insert_op(cur_db, op: MemoryOp, version: int, *, reason: str,
                         decision: str) -> None:
        from psycopg.types.json import Jsonb

        await cur_db.execute(
            "INSERT INTO memory_ops (op_id, ts, user_id, session_id, action, key, value, "
            "type, source, confidence, actor, reason, decision, version, evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (op_id) DO NOTHING",
            (op.op_id, datetime.fromtimestamp(op.ts, timezone.utc), op.user_id,
             op.session_id, op.action, op.key,
             Jsonb(op.value) if op.value is not None else None,
             op.type, op.source, op.confidence, op.actor, reason, decision,
             version, Jsonb(op.evidence)))

    async def _ensure_wal_group(self) -> None:
        """确保 WAL 消费组存在（MKSTREAM）。组丢失（如 stream 被清）时自愈重建。"""
        r = await self._ensure_redis()
        try:
            await r.xgroup_create(name=settings.memory_wal_stream,
                                  groupname=settings.memory_wal_group,
                                  id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP：已存在，忽略
            if "BUSYGROUP" not in str(exc):
                raise

    async def fold_once(self, count: int = 100) -> Dict[str, Any]:
        """消费一批 WAL 并折进 PG。返回处理条数（幂等，可反复调）。"""
        mode = await self.mode()
        if mode != "redis":
            return {"folded": 0, "mode": mode, "note": "pg_direct 模式无 WAL 待折"}
        r = await self._ensure_redis()
        consumer = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        try:
            msgs = await r.xreadgroup(settings.memory_wal_group, consumer,
                                      {settings.memory_wal_stream: ">"}, count=count)
        except Exception as exc:
            if "NOGROUP" not in str(exc):
                raise
            await self._ensure_wal_group()      # 组丢失：重建后重试一次
            msgs = await r.xreadgroup(settings.memory_wal_group, consumer,
                                      {settings.memory_wal_stream: ">"}, count=count)
        if not msgs:
            return {"folded": 0, "mode": mode}
        folded = 0
        for _stream, entries in msgs:
            for msg_id, fields in entries:
                try:
                    op = MemoryOp(**json.loads(fields["op"]))
                    await self._fold_op(op)
                    await r.xack(settings.memory_wal_stream,
                                 settings.memory_wal_group, msg_id)
                    folded += 1
                except Exception as exc:
                    self.stats["fold_fail"] += 1
                    logger.warning("[memory] WAL 折叠失败（不 ACK，下轮重试）：%s", exc)
                    return {"folded": folded, "mode": mode, "error": str(exc)}
        self.stats["folded"] += folded
        return {"folded": folded, "mode": mode}

    async def fold_loop(self, block_ms: int = 2000) -> None:
        """常驻 worker：阻塞读 WAL 折进 PG。由调用方负责取消（task.cancel）。"""
        r = await self._ensure_redis()
        consumer = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        logger.info("[memory] fold worker 启动（consumer=%s）", consumer)
        while True:
            try:
                msgs = await r.xreadgroup(settings.memory_wal_group, consumer,
                                          {settings.memory_wal_stream: ">"},
                                          count=50, block=block_ms)
            except Exception as exc:
                if "NOGROUP" not in str(exc):
                    raise
                await self._ensure_wal_group()   # 组丢失：重建后继续
                continue
            if not msgs:
                continue
            for _stream, entries in msgs:
                for msg_id, fields in entries:
                    try:
                        op = MemoryOp(**json.loads(fields["op"]))
                        await self._fold_op(op)
                        await r.xack(settings.memory_wal_stream,
                                     settings.memory_wal_group, msg_id)
                        self.stats["folded"] += 1
                    except Exception as exc:
                        self.stats["fold_fail"] += 1
                        logger.warning("[memory] WAL 折叠失败（不 ACK）：%s", exc)

    # ------------------------------------------------------------------ 读取
    async def recall_items(self, user_id: str, keys: Optional[List[str]] = None,
                           limit: int = 200) -> List[MemoryItem]:
        """召回当前有效记忆。Redis 模式读热层；热层为空时从 PG 回填（自愈）。"""
        mode = await self.mode()
        items: List[MemoryItem] = []
        if mode == "redis":
            try:
                r = await self._ensure_redis()
                raw = await r.hgetall(self._user_hash(user_id))
                if raw:
                    items = [MemoryItem(**_item_from_dict(d)) for d in
                             (json.loads(v) for v in raw.values())]
                else:
                    items = await self._recall_pg(user_id, limit)
                    if items:
                        pipe = r.pipeline(transaction=False)
                        for it in items:
                            pipe.hset(self._user_hash(user_id), it.key,
                                      json.dumps(_item_to_dict(it), ensure_ascii=False))
                        await pipe.execute()
                        logger.info("[memory] 热层为空，已从 PG 回填 %d 条", len(items))
            except Exception as exc:
                logger.warning("[memory] 热层读取失败，回退 PG：%s", exc)
                items = await self._recall_pg(user_id, limit)
        else:
            items = await self._recall_pg(user_id, limit)
        items = [i for i in items if i.status == STATUS_VALID]
        if keys:
            wanted = set(keys)
            items = [i for i in items if i.key in wanted]
        items.sort(key=lambda x: -(x.updated_at or 0))
        return items[:limit]

    async def _recall_pg(self, user_id: str, limit: int) -> List[MemoryItem]:
        pg = await self._ensure_pg()
        rows = await pg.afetch(
            "SELECT * FROM memory_items WHERE user_id = %s AND status = %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (user_id, STATUS_VALID, limit))
        return [_row_to_item(r) for r in rows]

    async def history(self, user_id: str, key: Optional[str] = None,
                      limit: int = 100) -> Dict[str, Any]:
        """审计读取：条目全史（含 superseded/deleted）+ 操作日志。"""
        pg = await self._ensure_pg()
        if key:
            rows = await pg.afetch(
                "SELECT * FROM memory_items WHERE user_id = %s AND key = %s "
                "ORDER BY recorded_at DESC LIMIT %s", (user_id, key, limit))
            ops = await pg.afetch(
                "SELECT * FROM memory_ops WHERE user_id = %s AND key = %s "
                "ORDER BY ts DESC LIMIT %s", (user_id, key, limit))
        else:
            rows = await pg.afetch(
                "SELECT * FROM memory_items WHERE user_id = %s "
                "ORDER BY recorded_at DESC LIMIT %s", (user_id, limit))
            ops = await pg.afetch(
                "SELECT * FROM memory_ops WHERE user_id = %s "
                "ORDER BY ts DESC LIMIT %s", (user_id, limit))
        return {"items": rows, "ops": ops}

    async def export_user(self, user_id: str) -> Dict[str, Any]:
        """合规导出：当前生效值（含未折进 PG 的热层待写）+ 全史 + 操作日志。"""
        current = await self.recall_items(user_id)
        hist = await self.history(user_id, limit=1000)
        return {"user_id": user_id,
                "exported_ts": datetime.now(timezone.utc).isoformat(),
                "current": [_item_to_dict(i) for i in current],
                "history_items": [_jsonable(r) for r in hist["items"]],
                "ops": [_jsonable(r) for r in hist["ops"]]}

    async def erase_user(self, user_id: str, actor: str = "用户") -> Dict[str, Any]:
        """合规擦除：全部有效记忆软删除（墓碑 + 审计）。物理清除由后续作业执行。"""
        items = await self.recall_items(user_id)
        existing = {i.key: i for i in items}
        ops = [MemoryOp(op_id=uuid.uuid4().hex, action=OP_ERASE, key=i.key,
                        value=None, actor=actor) for i in items]
        if not ops:
            return {"erased": 0, "user_id": user_id}
        return {"erased": len(ops), "user_id": user_id,
                **await self.commit(user_id, [(o, KEEP, "erase_user") for o in ops],
                                    existing, session_id=f"erase-{int(datetime.now(timezone.utc).timestamp())}")}

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa
                pass
            self._redis = None
        if self._pg is not None:
            await self._pg.aclose()
            self._pg = None


# ------------------------------------------------------------------ 序列化
def _item_to_dict(item: MemoryItem) -> Dict[str, Any]:
    from dataclasses import asdict

    return asdict(item)


def _item_from_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Redis JSON -> MemoryItem kwargs（容错：缺字段用默认值）。"""
    from dataclasses import fields

    names = {f.name for f in fields(MemoryItem)}
    return {k: v for k, v in d.items() if k in names}


def _row_to_item(row: Dict[str, Any]) -> MemoryItem:
    """PG 行 -> MemoryItem（datetime -> epoch 秒）。"""
    def ts(v):
        return v.timestamp() if hasattr(v, "timestamp") else (float(v) if v else 0.0)

    return MemoryItem(
        key=row["key"], value=row["value"], type=row["type"],
        version=int(row["version"]), status=row["status"],
        valid_from=ts(row.get("valid_from")),
        valid_to=ts(row.get("valid_to")) or None,
        recorded_at=ts(row.get("recorded_at")), updated_at=ts(row.get("updated_at")),
        source=row.get("source") or "explicit",
        confidence=float(row.get("confidence") or 1.0),
        evidence=list(row.get("evidence") or []),
        pii_level=row.get("pii_level") or "internal",
        content_hash=row.get("content_hash") or "",
        user_id=row.get("user_id") or "", session_id=row.get("session_id") or "")


def _jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out
