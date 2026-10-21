"""交互式命令行（原型期最直观的"使用"入口）。

运行：
  python -m sca.cli          # 进入对话式 REPL

命令：
  直接输入一段异常描述        -> 自动意图识别 + 按推断等级跑状态机（含 P0/P1 审批）
  route <文本>               -> 仅看意图识别 + 工具路由
  trigger <等级> [文本]       -> 按指定等级(P0/P1/P2/P3)跑状态机，可选自定义描述
  eval                       -> 跑 L0/L1 模拟论证
  coverage                   -> 看 MCP 对 12 子系统覆盖
  demo                       -> 跑内置双场景
  help / exit

零依赖即可运行（意图模型无 Key 时走规则路由）。
"""
from __future__ import annotations

import asyncio
import re
import sys

_BANNER = """
╔══════════════════════════════════════════════════════════╗
  供应链多智能体调度与异常处理系统 · 交互控制台（原型期）
  直接描述一个异常即可看智能体如何识别意图 / 路由工具 / 决策
  输入 help 看全部命令，exit 退出
╚══════════════════════════════════════════════════════════╝
"""


def _infer_level(text: str) -> str:
    """从文本推断异常等级（原型期启发式；生产期由业务规则/SLA 决定）。"""
    if re.search(r"危险品|泄露|爆炸|车祸|事故|人员伤亡|重大|放射性|危化|火灾", text):
        return "P0"
    if re.search(r"海关|查验|扣留|扣关|清关|资质|证照|超限|载重", text):
        return "P1"
    if re.search(r"偏航|延误|温控|冷链|超时|故障|异常|投诉|破损", text):
        return "P2"
    return "P3"


def _print_result(result: dict) -> None:
    trace = result.get("trace") or []
    print("  执行轨迹:", " -> ".join(trace))
    if "replan_ms" in result and result["replan_ms"] is not None:
        print(f"  重规划耗时: {result['replan_ms']} ms  (L0 目标 <500ms)")
    if "plan_version" in result:
        print(f"  计划版本: v{result['plan_version']}")
    if result.get("sop"):
        print(f"  SOP 命中: {result['sop'].get('sop_id')} (match={result['sop'].get('match')})")


async def cmd_route(text: str) -> None:
    from sca.agents import TOOL_REGISTRY
    from sca.intent import build_intent_model

    model = build_intent_model()
    res = await model.classify(text, TOOL_REGISTRY, {})
    print(f"  意图(intent): {res.intent}")
    print(f"  置信度: {res.confidence}  来源: {res.source}")
    if res.tool_calls:
        print(f"  工具调用: {res.tool_calls}")
    else:
        print("  工具调用: (无 / 需模型兜底，未配置 API Key 时为空)")


async def cmd_trigger(level: str, text: str = "") -> None:
    from sca.agents import SupplyChainGraph
    from sca.intent import build_intent_model
    from sca.simulation import OrderGenerator, FaultInjector
    from sca._compat import Command, GraphInterrupt

    level = level.upper()
    g = SupplyChainGraph(intent_model=build_intent_model())
    graph = g.compile(interrupt_before=["approval_gate"])
    # 有需求文本则按文本落单；无文本时才是仿真压测入口（会明确标注）
    order = None
    if text:
        from sca.intake import parse_dispatch_request
        from sca.util.aio import run_blocking
        # 同步函数（模型兜底会发 HTTP）-> 线程池，避免阻塞 CLI 的事件循环
        pr = await run_blocking(parse_dispatch_request, text, use_model=True)
        if pr.get("ok") and pr["order"].get("origin") and pr["order"].get("dest"):
            order = pr["order"]
    if order is None:
        order = OrderGenerator().next()
        print("  [订单] 未提供可受理的需求文本，使用仿真订单（仅压测/演示）")
    anomaly = FaultInjector().inject(level)
    print(f"  [异常] code={anomaly.get('code')} level={level} desc={anomaly.get('desc')}")
    try:
        result = await graph.ainvoke(
            {"order": order, "exception": anomaly},
            config={"configurable": {"thread_id": "cli"}},
        )
        _print_result(result)
    except GraphInterrupt:
        print("  ⏸ 已挂起，等待人工审批（高危 P0/P1）。输入 approve 通过 / deny 拒绝：")
        ans = input("  审批> ").strip().lower()
        approved = ans in ("approve", "y", "yes", "通过", "ok")
        decision = {"approved": approved, "by": "cli-operator"}
        print(f"  审批结果: {'通过' if approved else '拒绝'}")
        result = await graph.ainvoke(
            Command(resume=decision),
            config={"configurable": {"thread_id": "cli"}},
        )
        _print_result(result)


async def cmd_free(text: str) -> None:
    level = _infer_level(text)
    print(f"[意图路由] 描述: {text}")
    await cmd_route(text)
    print(f"[状态机] 推断等级: {level}，进入处置流程 ——")
    await cmd_trigger(level, text)


async def _cmd_eval() -> None:
    from sca.eval.harness import run_all

    rep = await run_all()
    passed = sum(1 for m in rep["metrics"] if m.status == "pass")
    na = sum(1 for m in rep["metrics"] if m.status == "na")
    failed = sum(1 for m in rep["metrics"] if m.status == "fail")
    print(f"  L0/L1 指标：通过 {passed} / 未达标 {failed} / 需外部能力验收 {na}")
    for m in rep["metrics"]:
        flag = {"pass": "✅", "fail": "❌", "na": "⚠️"}.get(m.status, "")
        print(f"  {flag} {m.code} {m.name}: {m.actual}{m.unit} (目标 {m.target}{m.unit})")


async def _cmd_coverage() -> None:
    from sca.mcp.registry import coverage_summary

    cov = coverage_summary()
    print(f"  MCP Server {cov['server_count']} 个，覆盖子系统 {len(cov['subsystems'])} 个：")
    for s in cov["subsystems"]:
        print(f"   - {s}")


async def _cmd_demo() -> None:
    import sca.demo as demo

    await demo.main()


_HELP = """命令：
  <直接输入异常描述>   自动意图识别 + 按推断等级跑状态机（含 P0/P1 审批）
  route <文本>         仅看意图识别 + 工具路由
  trigger <等级> [文本] 按指定等级(P0/P1/P2/P3)跑状态机
  eval                 跑 L0/L1 模拟论证
  coverage             看 MCP 对 12 子系统覆盖
  demo                 跑内置双场景
  help                 显示本帮助
  exit                 退出"""


def main() -> None:
    print(_BANNER)
    while True:
        try:
            line = input("供应链> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not line:
            continue
        if line in ("exit", "quit", "q"):
            print("再见。")
            break
        if line == "help":
            print(_HELP)
            continue
        if line == "eval":
            asyncio.run(_cmd_eval())
            continue
        if line == "coverage":
            asyncio.run(_cmd_coverage())
            continue
        if line == "demo":
            asyncio.run(_cmd_demo())
            continue
        if line.startswith("route "):
            asyncio.run(cmd_route(line[6:].strip()))
            continue
        if line.startswith("trigger "):
            parts = line[len("trigger "):].split(None, 1)
            lvl = parts[0]
            txt = parts[1] if len(parts) > 1 else ""
            asyncio.run(cmd_trigger(lvl, txt))
            continue
        # 其余视为自由文本描述
        asyncio.run(cmd_free(line))


if __name__ == "__main__":
    main()
