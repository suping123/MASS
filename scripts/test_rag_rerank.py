"""RAG 重排（FR-10 第三阶段）验收。

    ./.venv/Scripts/python.exe scripts/test_rag_rerank.py

重排最容易假的地方是"加了但不影响结果"——所以这里不看"有没有调用"，而看
**排序有没有真的改变**：给长文档一个更高的融合分（模拟它靠篇幅堆词赢了召回），
断言重排后短而精确的那条排第一。另外把三档开关全测一遍：
`off` 必须与改造前逐位一致（回滚得回去），`api` 无 Key 必须静默降级而不是抛错。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.rag import rerank as R  # noqa: E402
from sca.rag.hybrid import Chunk, MockHybridRetriever  # noqa: E402

FAILS: list[str] = []
PASSED = 0
SKIPPED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {name}  （{why}）")


Q = "冷链断链怎么处置"

LONG = Chunk("sop-A",
             "本手册适用于冷链运输的各类场景，包括温控设备的日常检查、记录填写、"
             "温度记录仪的校准，以及在发生异常时的上报流程与责任划分，涉及调度、"
             "司机、收货人三方。此外还包括断链后的处置原则与赔付标准、保险理赔所需材料。")
SHORT = Chunk("sop-B", "冷链断链后 30 分钟内回温并拍照留证，超 2 小时整批拒收。")
FAR = Chunk("sop-C", "普货装卸作业规范：叉车限速 5 公里每小时，堆码不超过三层。")


def main() -> int:
    print("[A] 打分函数：四个特征各自能拉开差距")

    cov_full = R._coverage(set(R._terms("冷链断链")), "冷链断链处置")
    cov_part = R._coverage(set(R._terms("冷链断链")), "冷链运输的一般要求")
    check("覆盖率：全命中 > 部分命中", cov_full > cov_part, f"{cov_full} vs {cov_part}")

    q = set(R._terms(Q))
    d_short, d_long = R._density(q, SHORT.content), R._density(q, LONG.content)
    check("密度：短而富集 > 长而稀疏", d_short > d_long, f"{d_short:.3f} vs {d_long:.3f}")

    near_compact = R._proximity(list(R._terms(Q)), SHORT.content)
    near_scatter = R._proximity(list(R._terms(Q)), LONG.content)
    check("邻近度：紧凑成句 > 分散在段落各处", near_compact > near_scatter,
          f"{near_compact:.3f} vs {near_scatter:.3f}")

    f_hit = R._field("E-03 爆胎怎么处理", {"code": "E-03", "title": "高速爆胎"})
    f_miss = R._field("E-03 爆胎怎么处理", {"code": "E-07", "title": "货损"})
    check("字段命中：故障码对上 > 对不上", f_hit > f_miss, f"{f_hit} vs {f_miss}")

    check("空 query 原样返回首轮分", R.score("", SHORT.content, {}, 0.42) == 0.42,
          str(R.score("", SHORT.content, {}, 0.42)))

    print("\n[B] 排序纠正：把「靠篇幅赢了召回」的那条换下来")

    # 长文 base=0.82（模拟融合分第一），短文 base=0.61
    hits = [(LONG, 0.82), (SHORT, 0.61), (FAR, 0.05)]
    os.environ["SCA_RAG_RERANK"] = "local"
    out = R.rerank_chunks(Q, hits, 2)
    check("重排后短而精确的那条排第一", out[0][0].doc_id == "sop-B", str([(c.doc_id, s) for c, s in out]))
    check("结果条数不超过 top_k", len(out) <= 2, str(len(out)))
    check("不引入候选之外的文档", {c.doc_id for c, _ in out} <= {"sop-A", "sop-B", "sop-C"},
          str([c.doc_id for c, _ in out]))
    check("不相关的文档仍在最后", out[-1][0].doc_id != "sop-C" or len(out) == 1,
          str([c.doc_id for c, _ in out]))

    rows = [("sop-A", 0.82, {"content": LONG.content, "code": "SOP-A"}),
            ("sop-B", 0.61, {"content": SHORT.content, "code": "SOP-B"}),
            ("sop-C", 0.05, {"content": FAR.content, "code": "SOP-C"})]
    rows_out = R.rerank_rows(Q, rows, 2)
    check("PG 路径（rows）同样被纠正", rows_out[0][0] == "sop-B",
          str([(r[0], r[1]) for r in rows_out]))
    check("PG 路径返回结构仍是三元组", all(len(r) == 3 for r in rows_out), str(rows_out[:1]))

    print("\n[C] 三档开关：off 可回滚、local 可复现、api 会降级")

    os.environ["SCA_RAG_RERANK"] = "off"
    off = R.rerank_chunks(Q, hits, 2)
    check("off 模式原样返回融合分顺序", [c.doc_id for c, _ in off] == ["sop-A", "sop-B"],
          str([(c.doc_id, s) for c, s in off]))
    check("off 模式不改分数", [s for _, s in off] == [0.82, 0.61], str([s for _, s in off]))

    os.environ["SCA_RAG_RERANK"] = "local"
    a = R.rerank_chunks(Q, hits, 2)
    b = R.rerank_chunks(Q, hits, 2)
    check("local 模式两次调用结果一致（确定性）",
          [(c.doc_id, s) for c, s in a] == [(c.doc_id, s) for c, s in b], str(a))

    os.environ["SCA_RAG_RERANK"] = "bogus"
    check("非法档位按 local 处理（配错不哑）", R.mode() == "local", R.mode())

    os.environ["SCA_RAG_RERANK"] = "api"
    orig = R._api_rerank
    R._api_rerank = lambda *a, **k: None      # 模拟无 Key / 超时 / 5xx
    try:
        got = R.rerank_chunks(Q, hits, 2)
        check("api 不可用时静默降级，不抛异常", len(got) > 0, str(got))
        check("降级后结果与 local 一致", [(c.doc_id, s) for c, s in got] ==
              [(c.doc_id, s) for c, s in a], str(got))
    except Exception as exc:                  # noqa: BLE001
        check("api 不可用时静默降级，不抛异常", False, repr(exc))
    finally:
        R._api_rerank = orig

    print("\n[D] 边界与集成")

    os.environ["SCA_RAG_RERANK"] = "local"
    check("空候选不炸", R.rerank_chunks(Q, [], 3) == [])
    one = R.rerank_chunks(Q, [(SHORT, 0.5)], 3)
    check("单条候选原样返回", len(one) == 1 and one[0][0].doc_id == "sop-B", str(one))

    r = MockHybridRetriever()
    r.add([LONG, SHORT, FAR])
    got = r.search(Q, top_k=2)
    check("内存检索集成后仍返回不超过 top_k 条", 0 < len(got) <= 2, str(len(got)))
    check("集成后分数在 0-1 之间", all(0.0 <= s <= 1.0 for _, s in got),
          str([s for _, s in got]))
    check("集成后首条是短而精确那条", got[0][0].doc_id == "sop-B",
          str([(c.doc_id, s) for c, s in got]))
    empty = MockHybridRetriever()
    empty.add([FAR])
    check("完全不相关的查询不返回垃圾", empty.search("量子计算", top_k=2) == [],
          str(empty.search("量子计算", top_k=2)))

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        print(f"（通过 {PASSED} 项，跳过 {SKIPPED} 项）")
        return 1
    print(f"PASS：{PASSED} 项断言全部通过（重排真的改变了排序，且可回滚）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
