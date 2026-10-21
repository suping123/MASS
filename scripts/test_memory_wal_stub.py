"""WAL 异步写入路径的桩验证（fakeredis 替代真 Redis）。

为什么需要这个脚本：本机没有 redis-server 时，scripts/test_memory.py 只能
验证 pg_direct 模式；而 WAL 折叠（写穿透 -> stream -> 显式事务 fold ->
幂等重投）是整套异步写入里最容易出错的一段，值得用桩全覆盖。

覆盖：
  W1  redis 模式提交：热层 hash 立即可读（读个人写）+ WAL stream 有条目
  W2  fold 前 PG 无记录；fold 后落库（同键 supersede + 新版 version+1 + 审计）
  W3  重复 fold 幂等（op_id 查重，不重复 supersede/insert）
  W4  更新链路：v1 短信 -> v2 电话，fold 后 PG 双版本 + valid_to 正确
  W5  Redis 写入中途失败 -> 回滚热层 + 就地 fold 进 PG（不丢、不双重生效）
  W6  delete/erase 走 WAL fold：PG 置 deleted + 审计

用法（PG 需在跑；本脚本只管 Redis 桩）：
    .venv\\Scripts\\python.exe scripts/test_memory_wal_stub.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 项目惯例：脚本可导入 sca

from sca import api  # noqa: E402,F401  先 import：设置 Windows Selector 循环策略
from sca import memory  # noqa: E402
from sca.memory import store as store_mod  # noqa: E402
from sca.memory.models import MemoryOp  # noqa: E402
from sca.memory.resolve import KEEP  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"{'  [PASS]' if cond else '  [FAIL]'} {name}" + (f" — {extra}" if extra else ""))
    return cond


async def main() -> int:
    import fakeredis.aioredis as fake_aioredis

    fake = fake_aioredis.FakeRedis(decode_responses=True)
    store = memory.get_store()

    # 用桩替换 _ensure_redis，并强制模式为 redis（绕过真机探测）
    async def _fake_ensure_redis():
        return fake
    store._ensure_redis = _fake_ensure_redis  # type: ignore[assignment]
    store._mode = "redis"
    store._mode_checked_at = time.time()

    pg = await store._ensure_pg()
    ts = int(time.time())
    user = f"test-wal-{ts}"
    hkey = f"mem:{user}"
    stream = "mem:wal"

    print("=" * 74)
    print(f"WAL 桩验证（fakeredis）· user={user}")
    print("=" * 74)

    # ---------------------------------------------------------------- W1
    r1 = await memory.observe_turn(f"api-{ts:08x}a", user, "以后通知都用短信")
    check("W1a 提交路径走 redis+wal",
          all(c["path"] == "redis+wal" for c in r1["committed"]), f"{r1['committed']}")
    hot = await fake.hget(hkey, "preference.notify_channel")
    check("W1b 热层写穿透立即可读（读个人写）", hot is not None
          and '"value": "短信"' in hot.replace("'", '"') or (hot and "短信" in hot))
    wal_len = await fake.xlen(stream)
    check("W1c WAL stream 有条目", wal_len >= 1, f"len={wal_len}")

    # ---------------------------------------------------------------- W2/W3
    before = await pg.afetch_one(
        "SELECT count(*) AS n FROM memory_items WHERE user_id = %s", (user,))
    check("W2a fold 前 PG 无记录（异步窗口）", before["n"] == 0, f"n={before['n']}")
    f1 = await memory.fold_once()
    after = await pg.afetch_one(
        "SELECT count(*) AS n FROM memory_items WHERE user_id = %s", (user,))
    ops1 = await pg.afetch_one(
        "SELECT count(*) AS n FROM memory_ops WHERE user_id = %s", (user,))
    check("W2b fold 后落库（1 条有效）", f1.get("folded") == 1 and after["n"] == 1
          and ops1["n"] == 1, f"folded={f1.get('folded')} items={after['n']} ops={ops1['n']}")
    f2 = await memory.fold_once()
    ops2 = await pg.afetch_one(
        "SELECT count(*) AS n FROM memory_ops WHERE user_id = %s", (user,))
    check("W3 重复 fold 幂等（op_id 查重，无重复行）", ops2["n"] == 1
          and f2.get("folded") == 0, f"ops={ops2['n']} folded={f2.get('folded')}")

    # ---------------------------------------------------------------- W4 更新链路
    await memory.observe_turn(f"api-{ts:08x}b", user, "以后通知改用电话")
    await memory.fold_once()
    rows = await pg.afetch(
        "SELECT version, status, value, valid_to FROM memory_items "
        "WHERE user_id = %s AND key = 'preference.notify_channel' "
        "ORDER BY version", (user,))
    check("W4a fold 后双版本（v1 取代 / v2 有效）",
          len(rows) == 2 and rows[0]["status"] == "superseded"
          and rows[1]["status"] == "valid" and rows[1]["version"] == 2)
    check("W4b 旧版 valid_to 非空、新版值正确",
          rows[0]["valid_to"] is not None and rows[1]["value"] == "电话")

    # ---------------------------------------------------------------- W5 故障回退
    class _BoomRedis:
        """模拟 Redis 在写穿透后、WAL append 前挂掉。"""

        def __init__(self, inner):
            self._inner = inner
            self.hset_calls = 0

        async def hset(self, *a, **k):
            self.hset_calls += 1
            return await self._inner.hset(*a, **k)

        async def hdel(self, *a, **k):
            return await self._inner.hdel(*a, **k)

        async def xadd(self, *a, **k):
            raise ConnectionError("simulated wal failure")

        async def xreadgroup(self, *a, **k):
            return await self._inner.xreadgroup(*a, **k)

        async def xack(self, *a, **k):
            return await self._inner.xack(*a, **k)

        async def xgroup_create(self, *a, **k):
            return await self._inner.xgroup_create(*a, **k)

    boom = _BoomRedis(fake)
    user5 = f"test-wal-fail-{ts}"

    async def _boom_ensure_redis():
        return boom
    store._ensure_redis = _boom_ensure_redis  # type: ignore[assignment]  换上故障桩

    existing = {}
    op = MemoryOp(op_id=f"boom-{ts}", action="upsert",
                  key="preference.language", value="英语", type="preference")
    out5 = await store.commit(user5, [(op, KEEP, "test")], existing, "api-boom")
    row5 = await pg.afetch_one(
        "SELECT value, version FROM memory_items "
        "WHERE user_id = %s AND key = 'preference.language' AND status = 'valid'",
        (user5,))
    hot5 = await fake.hget(f"mem:{user5}", "preference.language")
    check("W5a WAL 失败回退 pg_direct：PG 就地落库", row5 is not None
          and row5["value"] == "英语" and row5["version"] == 1,
          f"row={row5}")
    check("W5b 热层已回滚（无幽灵条目）", hot5 is None)
    check("W5c 提交摘要标记 fallback 路径",
          out5["committed"][0]["path"] == "pg_direct(fallback)")

    # 恢复正常桩，验证 delete 走 WAL -> fold 的完整路径
    store._ensure_redis = _fake_ensure_redis  # type: ignore[assignment]

    # ---------------------------------------------------------------- W6 delete/erase
    await memory.observe_turn(f"api-{ts:08x}c", user5, "以后通知都用邮件")
    await memory.fold_once()
    await memory.observe_turn(f"api-{ts:08x}d", user5, "别再给我发邮件通知")
    await memory.fold_once()
    rows6 = await pg.afetch(
        "SELECT status, valid_to FROM memory_items "
        "WHERE user_id = %s AND key = 'preference.notify_channel'", (user5,))
    check("W6a delete 走 WAL fold 置 deleted", any(
        r["status"] == "deleted" and r["valid_to"] is not None for r in rows6))
    ops6 = await pg.afetch_one(
        "SELECT count(*) AS n FROM memory_ops WHERE user_id = %s AND action = 'delete'",
        (user5,))
    check("W6b delete 审计留痕", ops6["n"] >= 1, f"n={ops6['n']}")

    # ---------------------------------------------------------------- 清理
    await memory.erase_user(user)
    await memory.erase_user(user5)
    await memory.fold_once()
    await store.close()

    print("=" * 74)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for f in FAIL:
            print(f"  [FAIL] {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
