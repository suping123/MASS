"""垫片 Command 动态路由的双引擎等价性验证（S1 契约层）。

为什么必须单独测：`Command(goto=...)` 是从"静态条件边"转向"动态路由"的地基。垫片原先
只是**定义了** Command 却完全忽略它的 goto（`_apply_update` 只认 dict，非 dict 直接 return），
于是节点返回的 Command 会被**静默丢弃**：官方引擎跳转、垫片继续走静态边。后果不是报错，
而是双引擎等价性对照（verify_p0p1.py --shim，过去两轮证明 P0/P1 无回归的唯一证据链）
无声失效。

本脚本用**同一份图定义**在两个引擎上各跑一遍，逐场景比对路由结果。用真实图而不是读源码
推断，是因为"节点既有静态出边又返回 Command"时官方到底是覆盖还是叠加，只有实测才能确定。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/test_compat_command.py            # 跑双引擎并比对
  .venv/Scripts/python.exe -X utf8 scripts/test_compat_command.py --engine shim
"""
import argparse
import json
import operator
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, List, TypedDict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESULT_MARK = "__COMPAT_RESULT__"


class S(TypedDict, total=False):
    log: Annotated[List[str], operator.add]


def _block_langgraph() -> None:
    """屏蔽 langgraph，强制走零依赖垫片。"""
    import importlib.abc

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "langgraph" or fullname.startswith("langgraph."):
                raise ImportError("blocked for shim verification")
            return None

    sys.meta_path.insert(0, _Block())


def _run_graph(build) -> dict:
    """跑一张图，返回 {"log": [...]} 或 {"error": "..."}。"""
    import sca._compat as C

    try:
        g, _expected_terminal = build(C.StateGraph, C.END)
        app = g.compile()
        out = _asyncio_run(app.ainvoke({"log": []}, {"recursion_limit": 200}))
        log = list((out or {}).get("log") or [])
        return {"log": log}
    except Exception as exc:  # noqa
        return {"error": f"{type(exc).__name__}: {exc}"}


def _asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 场景：每个都返回 (StateGraph, 说明)
# ---------------------------------------------------------------------------
def sc_single(StateGraph, END):
    """Command 单目标跳转 + update 合并（路由节点无静态出边）。"""
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(goto="b", update={"log": ["sup"]}))
    g.add_node("b", lambda s: {"log": ["b"]})
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    return g, "a -> sup(=goto b) -> b"


def sc_fanout(StateGraph, END):
    """Command 多目标扇出（同超步并行派发两支）。"""
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(goto=["b", "c"], update={"log": ["sup"]}))
    g.add_node("b", lambda s: {"log": ["b"]})
    g.add_node("c", lambda s: {"log": ["c"]})
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    return g, "a -> sup(=goto [b, c])"


def sc_goto_end(StateGraph, END):
    """goto=END：终止动态目标，但**不撤销**静态出边（实测官方为叠加语义）。"""
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(goto=END, update={"log": ["sup"]}))
    g.add_node("b", lambda s: {"log": ["b"]})
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    g.add_edge("sup", "b")        # 静态边仍在，因此 b 依旧执行
    return g, "a -> sup(=goto END, 静态边 sup->b 仍生效)"


def sc_static_plus_goto(StateGraph, END):
    """⚠️ 探测题（已实测）：节点**既有**静态出边又返回 Command(goto=...)。

    官方结果 = ["a","sup","x","y"] → **两条路都走**。所以 goto 不是"覆盖静态边"，
    而是"在静态边之外追加目标"。设计推论：要让 Command 独占路由权，该节点就不能
    声明静态出边（supervisor 正是这样设计的）。
    """
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(goto="y", update={"log": ["sup"]}))
    g.add_node("x", lambda s: {"log": ["x"]})
    g.add_node("y", lambda s: {"log": ["y"]})
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    g.add_edge("sup", "x")        # 静态出边 -> x
    return g, "a -> sup(静态边->x 且 goto=y)：x 与 y 都执行"


def sc_unknown_target(StateGraph, END):
    """goto 指向不存在的节点：**两个引擎都静默忽略**（不报错、图提前终止）。

    这是官方行为（只往 branch 通道写，无消费者的写不报错）。垫片照抄以免分叉。
    安全后果：目标写错 = 图静默提前结束，所以目标合法性**必须**由上层在返回 Command
    之前校验（supervisor 的 precondition/ELIGIBLE 表），不能指望框架兜底。
    """
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(goto="nope", update={"log": ["sup"]}))
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    return g, "a -> sup(=goto 不存在的 nope)：两引擎均静默终止"


def sc_update_only(StateGraph, END):
    """Command 不带 goto（官方默认 goto=()）：应退回静态出边，不能终止。"""
    g = StateGraph(S)
    g.add_node("a", lambda s: {"log": ["a"]})
    g.add_node("sup", lambda s: _cmd(update={"log": ["sup"]}))
    g.add_node("b", lambda s: {"log": ["b"]})
    g.set_entry_point("a")
    g.add_edge("a", "sup")
    g.add_edge("sup", "b")
    return g, "a -> sup(=Command(update only), 静态边->b)"


SCENARIOS = {
    "single": sc_single,
    "fanout": sc_fanout,
    "goto_end": sc_goto_end,
    "static_plus_goto": sc_static_plus_goto,
    "update_only": sc_update_only,
    "unknown_target": sc_unknown_target,
}


def _cmd(**kw):
    """构造当前引擎的 Command（垫片 / 官方各一份，签名兼容）。"""
    import sca._compat as C
    if "goto" in kw and kw["goto"] is None:
        kw.pop("goto")
    return C.Command(**kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["real", "shim"], default="")
    args = ap.parse_args()

    if args.engine == "shim":
        _block_langgraph()

    import sca._compat as C  # noqa: E402

    engine = "real" if C.USING_REAL_LANGGRAPH else "shim"
    if args.engine and args.engine != engine:
        print(f"[compat-test] 引擎请求 {args.engine} 但实际 {engine}", file=sys.stderr)
        return 2

    results = {name: _run_graph(fn) for name, fn in SCENARIOS.items()}
    payload = {"engine": engine, "results": results}
    print(RESULT_MARK + json.dumps(payload, ensure_ascii=False))

    # 单引擎模式：把人类可读的结论也打出来
    if args.engine:
        print(f"\n[compat-test] 引擎 = {engine}")
        for name, res in results.items():
            note = SCENARIOS[name].__doc__ or ""
            print(f"  {name:18s} {json.dumps(res, ensure_ascii=False)}   {note.strip()}")
    return 0


def _run_child(engine: str) -> dict:
    env = dict(os.environ)
    env.update({"SCA_USE_PG": "false", "SCA_MEMORY_ENABLED": "false",
                "SCA_NOTIFY_DESKTOP": "false", "SCA_NOTIFY_DRY_RUN": "true"})
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--engine", engine],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
    for line in (proc.stdout or "").splitlines():
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    return {"engine": engine, "results": {},
            "fatal": f"子进程 {engine} 未产出结果 (rc={proc.returncode})\n"
                     f"stdout={proc.stdout[-800:]}\nstderr={proc.stderr[-800:]}"}


if __name__ == "__main__":
    argv_engine = ""
    for i, a in enumerate(sys.argv):
        if a == "--engine" and i + 1 < len(sys.argv):
            argv_engine = sys.argv[i + 1]
    if argv_engine:                       # 子进程模式：只跑单引擎
        raise SystemExit(main())

    real = _run_child("real")
    shim = _run_child("shim")

    if real.get("fatal") or shim.get("fatal"):
        print(real.get("fatal") or shim.get("fatal"))
        raise SystemExit(2)

    names = list(SCENARIOS)
    diff = 0
    print(f"\n{'场景':20s} {'官方 langgraph':32s} {'零依赖垫片':32s} 一致")
    print("-" * 92)
    for name in names:
        r = real["results"].get(name, {})
        s = shim["results"].get(name, {})
        same = (r == s)
        if not same:
            diff += 1
        print(f"{name:20s} {json.dumps(r, ensure_ascii=False):32s} "
              f"{json.dumps(s, ensure_ascii=False):32s} {'OK' if same else 'DIFF'}")
    print("-" * 92)
    print(f"[compat-test] 合计 {len(names)} 个场景，{diff} 处不一致")

    # 硬断言：新能力必须成立，否则等于"动态路由在垫片上静默失效"。
    # 期望值全部来自**官方引擎实测**，不是推断（见各场景 docstring）。
    fails = []
    checks = [
        ("single", ["a", "sup", "b"], "Command 单目标 + update"),
        ("fanout", ["a", "sup", "b", "c"], "Command 多目标扇出"),
        # 叠加语义：goto=END 不撤销静态边，b 依旧执行
        ("goto_end", ["a", "sup", "b"], "goto=END 不撤销静态出边"),
        # 叠加语义：静态边 x 与动态目标 y 都执行
        ("static_plus_goto", ["a", "sup", "x", "y"], "静态边与 goto 叠加"),
        # 无 goto 时退回静态边
        ("update_only", ["a", "sup", "b"], "Command(update only) 退回静态边"),
        # 未知目标被静默忽略，图提前终止
        ("unknown_target", ["a", "sup"], "未知 goto 目标静默忽略"),
    ]
    for name, want, desc in checks:
        got = real["results"].get(name, {})
        if got.get("log") != want:
            fails.append(f"{name}({desc}) 期望 {want} 实得 {got}")
    for f in fails:
        print("[compat-test] FAIL " + f)
    if diff == 0 and not fails:
        print("[compat-test] PASS 双引擎路由行为一致，且断言全部成立")
        raise SystemExit(0)
    raise SystemExit(1)
