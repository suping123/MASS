"""长短期记忆端到端自检（进程内直调 sca.memory，不 mock 存储）。

覆盖：
  T0  Guard 白名单 + 抽取器边界（未注册键丢弃、无关文本零提案）
  T1  会话内读个人写（本轮陈述 -> 立即召回）
  T2  跨会话延续（同 user_id 新会话召回）
  T3  更新与冲突消解（同键覆盖：新版生效、旧版 superseded+valid_to、双版本历史）
  T4  WAL 折叠（redis 模式：fold 前 PG 无记录、fold 后有、重复 fold 幂等；
      pg_direct 模式：提交即落 PG，本条自动降级验证）
  T5  审计 / 导出 / 擦除（export 全量、erase 软删+墓碑、召回为空）
  T6  降级路径（强制 pg_direct：写入与读取仍正确）
  T7  脱敏硬约束（手机号值在唯一写盘路口被打码）
  T8  挂钩接线（safe_observe_turn/safe_recall_blocks + _dispatch_flow 签名含 user_id）

用法：
    .venv\\Scripts\\python.exe scripts/test_memory.py

注意：先 `from sca import api` —— 它 import 时设置 WindowsSelectorEventLoopPolicy，
否则 psycopg 异步驱动在 Proactor 循环下建不上连（本项目既有约定）。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 项目惯例：脚本可导入 sca

# 必须先 import api：设置 Windows Selector 事件循环策略（PG 异步必需）
from sca import api  # noqa: E402,F401
from sca import memory  # noqa: E402
from sca.memory.extract import RuleExtractor, guard  # noqa: E402
from sca.memory.models import OP_UPSERT, MemoryOp  # noqa: E402
from sca.memory.resolve import KEEP  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"{'  [PASS]' if cond else '  [FAIL]'} {name}" + (f" — {extra}" if extra else ""))
    return cond


async def main() -> int:
    if not memory.enabled():
        print("[FATAL] 记忆子系统未启用：需要 SCA_USE_PG=true 且 SCA_MEMORY_ENABLED=true")
        return 2

    ts = int(time.time())
    user = f"test-mem-{ts}"
    user_pg = f"test-mem-pg-{ts}"
    store = memory.get_store()
    mode = await store.mode()
    print("=" * 74)
    print(f"长短期记忆自检 · mode={mode} · user={user}")
    print("=" * 74)
    print(f"[info] Redis 可达={mode == 'redis'}（不可达则自动走 pg_direct，"
          f"启动 Redis 后重跑可验证 WAL 路径）")

    # ---------------------------------------------------------------- T0 边界
    ops = RuleExtractor().propose("今天天气不错，随便聊聊")
    check("T0a 无关文本零提案", len(ops) == 0, f"ops={len(ops)}")
    ops = RuleExtractor().propose("以后通知都用短信")
    check("T0b 通知偏好的中文表述可抽取", len(ops) == 1
          and ops[0].key == "preference.notify_channel" and ops[0].value == "短信",
          f"{[(o.key, o.value) for o in ops]}")
    kept, dropped = guard([MemoryOp(op_id="x1", action=OP_UPSERT,
                                    key="not.a.registered.key", value="v")])
    check("T0c Guard 丢弃未注册键", len(kept) == 0 and len(dropped) == 1
          and dropped[0].reason == "guard_unregistered_key")

    # ---------------------------------------------------------------- T1 读个人写
    sid1 = f"api-{ts:08x}a"
    r1 = await memory.observe_turn(sid1, user, "以后通知都用短信")
    check("T1a 本轮偏好提案被提交", r1.get("committed") and
          any(c["key"] == "preference.notify_channel" for c in r1["committed"]),
          f"mode={r1.get('mode')}")
    blocks = await memory.recall_blocks(sid1, user)
    got = {b["key"]: b["value"] for b in blocks}
    check("T1b 会话内读个人写（提交后立即可召回）",
          got.get("preference.notify_channel") == "短信", f"blocks={got}")

    # ---------------------------------------------------------------- T2 跨会话
    sid2 = f"api-{ts:08x}b"
    blocks2 = await memory.recall_blocks(sid2, user)
    got2 = {b["key"]: b["value"] for b in blocks2}
    check("T2 跨会话延续（同 user_id 新会话召回）",
          got2.get("preference.notify_channel") == "短信", f"blocks={got2}")

    # ---------------------------------------------------------------- T3 冲突消解
    r3 = await memory.observe_turn(sid2, user, "以后通知改用电话")
    check("T3a 更新提案被提交", any(
        c["key"] == "preference.notify_channel" for c in r3.get("committed", [])))
    blocks3 = await memory.recall_blocks(sid2, user)
    got3 = {b["key"]: b["value"] for b in blocks3}
    check("T3b 新值生效、旧值被取代", got3.get("preference.notify_channel") == "电话")
    if mode == "redis":
        # redis 模式下 PG 审计视图按设计滞后于 WAL（异步窗口），断言前先折平
        await memory.fold_once()
    hist = await memory.history(user, "preference.notify_channel")
    items = hist["items"]
    valid = [i for i in items if i["status"] == "valid"]
    superseded = [i for i in items if i["status"] == "superseded"]
    check("T3c 历史双版本：1 有效 + 1 被取代",
          len(valid) == 1 and len(superseded) == 1 and valid[0]["version"] == 2
          and superseded[0]["version"] == 1, f"valid={len(valid)} sup={len(superseded)}")
    check("T3d 旧版带 valid_to（双时态，可回答'当时知道什么'）",
          superseded and superseded[0].get("valid_to") is not None)
    check("T3e 操作日志留痕（审计）", len(hist["ops"]) >= 2,
          f"ops={len(hist['ops'])}")

    # ---------------------------------------------------------------- T4 WAL 折叠
    # 用一个全新用户，避免 T3 的 fold 已把主测试用户的 WAL 折平
    user_wal = f"test-wal-{ts}"
    if mode == "redis":
        pg = store._pg_saver()
        await pg.ensure()
        await memory.observe_turn(f"api-{ts:08x}w", user_wal, "以后提醒都用钉钉")
        before = await pg.afetch_one(
            "SELECT count(*) AS n FROM memory_items WHERE user_id = %s", (user_wal,))
        check("T4a fold 前 PG 无记录（WAL 未折）", before["n"] == 0, f"n={before['n']}")
        f1 = await memory.fold_once()
        after = await pg.afetch_one(
            "SELECT count(*) AS n FROM memory_items WHERE user_id = %s", (user_wal,))
        ops1 = await pg.afetch_one(
            "SELECT count(*) AS n FROM memory_ops WHERE user_id = %s", (user_wal,))
        check("T4b fold 后落库（1 条有效 + 1 条审计）",
              f1.get("folded", 0) >= 1 and after["n"] == 1 and ops1["n"] == 1,
              f"folded={f1.get('folded')} items={after['n']} ops={ops1['n']}")
        f2 = await memory.fold_once()
        ops2 = await pg.afetch_one(
            "SELECT count(*) AS n FROM memory_ops WHERE user_id = %s", (user_wal,))
        check("T4c 重复 fold 幂等（op_id 查重，无重复行）",
              ops2["n"] == 1 and f2.get("folded") == 0,
              f"ops={ops2['n']} folded={f2.get('folded')}")
    else:
        pg = store._pg_saver()
        await pg.ensure()
        row = await pg.afetch_one(
            "SELECT count(*) AS n FROM memory_items WHERE user_id = %s "
            "AND status = 'valid'", (user,))
        check("T4d pg_direct 模式：提交即落 PG（无需 WAL）", row["n"] == 1,
              f"n={row['n']}")

    # ---------------------------------------------------------------- T5 导出/擦除
    exp = await memory.export_user(user)
    check("T5a 导出含当前生效值", any(
        i["key"] == "preference.notify_channel" and i["value"] == "电话"
        for i in exp["current"]))
    check("T5b 导出含全史与操作日志", len(exp["history_items"]) >= 2
          and len(exp["ops"]) >= 2)
    before_erase = await store.recall_items(user)
    er = await memory.erase_user(user)
    check("T5c 擦除软删除全部有效记忆", er.get("erased") == len(before_erase),
          f"erased={er.get('erased')} valid_before={len(before_erase)}")
    blocks5 = await memory.recall_blocks(sid2, user)
    check("T5d 擦除后召回为空", len(blocks5) == 0, f"blocks={len(blocks5)}")
    if mode == "redis":
        await memory.fold_once()   # 把擦除的墓碑折进 PG
    hist5 = await memory.history(user)
    check("T5e 墓碑留档（deleted 状态可查，物理清除由合规作业执行）",
          any(i["status"] == "deleted" for i in hist5["items"]))

    # ---------------------------------------------------------------- T6 降级路径
    old_use_redis = api.settings.use_redis if hasattr(api, "settings") else None
    from sca.settings import settings as _st
    _st.use_redis = False
    store._mode = None                      # 清模式缓存，强制重新判定
    r6 = await memory.observe_turn(f"api-{ts:08x}c", user_pg, "以后通知都用邮件")
    check("T6a pg_direct 强制模式：写入成功",
          any(c["key"] == "preference.notify_channel" and c["path"] == "pg_direct"
              for c in r6.get("committed", [])), f"mode={r6.get('mode')}")
    got6 = {b["key"]: b["value"] for b in await memory.recall_blocks("x", user_pg)}
    check("T6b pg_direct 强制模式：读取正确", got6.get("preference.notify_channel") == "邮件")
    _st.use_redis = True
    store._mode = None                      # 恢复探测

    # ---------------------------------------------------------------- T7 脱敏
    phone_val = "测试13812345678"
    op = MemoryOp(op_id=f"mask-{ts}", action=OP_UPSERT,
                  key="profile.display_name", value=phone_val)
    r7 = await store.commit(user_pg, [(op, KEEP, "test_mask")], {},
                            session_id=f"api-{ts:08x}d")
    items7 = await store.recall_items(user_pg, keys=["profile.display_name"])
    v7 = items7[0].value if items7 else ""
    check("T7 脱敏硬约束（手机号在写盘路口打码，不入明文）",
          "138****5678" in v7 and "13812345678" not in v7, f"value={v7}")

    # ---------------------------------------------------------------- T8 挂钩接线
    b8 = await memory.safe_recall_blocks(f"api-{ts:08x}e", user_pg)
    r8 = await memory.safe_observe_turn(f"api-{ts:08x}e", user_pg, "叫我老王")
    check("T8a safe_* 挂钩函数可用（fail-open 包装）", isinstance(b8, list)
          and isinstance(r8, dict))
    sig = inspect.signature(api._dispatch_flow)
    check("T8b _dispatch_flow 签名含 user_id（挂钩透传）", "user_id" in sig.parameters)

    # ---------------------------------------------------------------- 清理
    await memory.erase_user(user_pg)
    if mode == "redis":
        await memory.erase_user(user_wal)
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
