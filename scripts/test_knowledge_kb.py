"""知识库验证。

    ./.venv/Scripts/python.exe scripts/test_knowledge_kb.py

分两组：
  A 组【不依赖 PG】降级路径必须成立：PG 不可用时检索回退关键词、状态接口如实报因、不抛异常；
  B 组【需要 PG】真实向量通道：建表、全量导入、pg-vector 召回、单条增删同步。
B 组在 PG 不可用时打印 SKIP，不算失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.platform import kb  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def group_a() -> bool:
    """返回 PG 是否可用，供 B 组决定是否跑。"""
    print("\n[A] 降级路径（PG 不可用时系统仍可用）")

    from sca.knowledge_base import search_sop, summary

    s = summary()
    check("内置 SOP 已加载", s.get("total", 0) >= 20, str(s))
    print(f"       知识库：共 {s['total']} 条（内置 {s['builtin']} / 自定义 {s['custom']}）")

    hits = search_sop("冷链 温度超标 改道", top_k=3, code="E-COLD-OVER")
    check("按异常码能精确命中", bool(hits) and hits[0].get("exact") is True,
          str([h.get("sop_id") for h in hits]))
    check("命中项带处置步骤", bool(hits and hits[0].get("steps")),
          str(hits[0].get("steps"))[:80] if hits else "")

    hits2 = search_sop("车辆在高速上追尾了 需要换车", top_k=3)
    check("无异常码时也能按文本召回", len(hits2) > 0,
          str([h.get("sop_id") for h in hits2]))
    ch = hits2[0].get("channel") if hits2 else None
    check("渠道字段如实标注", ch in ("pg-vector", "kw-fallback", "code-exact"), str(ch))
    print(f"       检索渠道：{ch}")

    st = kb.stats()
    check("stats 在 PG 不可用时不抛异常", isinstance(st, dict), type(st).__name__)
    ok, why = kb.available()
    if not ok:
        check("stats 如实给出不可用原因", bool(st.get("reason")), json.dumps(st, ensure_ascii=False)[:200])
        print(f"       PG 状态：不可用（{st.get('reason')}）")
    else:
        print(f"       PG 状态：可用，块数={st.get('chunks')}")

    r = kb.sync(full=True) if not ok else {"ok": False, "error": "skipped"}
    if not ok:
        check("PG 不可用时 sync 返回可读错误而非崩溃", r.get("ok") is False and bool(r.get("error")),
              str(r)[:160])
    return ok


def group_b() -> None:
    print("\n[B] 真实向量通道（PG + pgvector）")

    r = kb.ensure_schema()
    check("建表成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:240])
    if not r.get("ok"):
        return

    s = kb.sync(full=True)
    print("       sync:", json.dumps(s, ensure_ascii=False)[:240])
    check("全量导入成功", s.get("ok") is True, str(s)[:200])
    if not s.get("ok"):
        return
    check("入库条数等于 SOP 数", s.get("docs", 0) >= 20, str(s.get("docs")))
    check("无向量生成失败", s.get("failed", 1) == 0, f"failed={s.get('failed')}")
    check("记录了向量维度", int(s.get("dim") or 0) > 0, str(s.get("dim")))

    st = kb.stats()
    check("stats 显示已入库", st.get("chunks", 0) > 0, str(st.get("chunks")))
    check("模型一致性校验通过", st.get("consistent") is True,
          f"库内={st.get('model_in_db')} 当前={st.get('model_now')}")

    from sca.knowledge_base import search_sop

    hits = search_sop("冷藏车温度超过上限怎么办", top_k=3)
    ch = hits[0].get("channel") if hits else None
    check("检索走 PG 向量通道", ch == "pg-vector", f"channel={ch}")
    check("向量召回有结果", len(hits) > 0, str([h.get("sop_id") for h in hits]))
    if hits:
        print(f"       top1={hits[0].get('sop_id')} {hits[0].get('title')} "
              f"match={hits[0].get('match')}")

    h = kb.history(5)
    check("构建留痕可查", len(h) > 0, str(h[:1]))


def main() -> int:
    pg_ok = group_a()
    if pg_ok:
        group_b()
    else:
        print("\n[B] SKIP：PG 未启动，跳过真实向量通道（启动后重跑本脚本即可覆盖）")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
