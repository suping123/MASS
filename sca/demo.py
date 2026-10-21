"""Phase-0 可运行 Demo（无 PG 也能跑，默认内存检查点）。

演示两条主链路：
  A) P2 异常 -> 自动换路重规划（验证 L0：重规划耗时，理论 <500ms）
  B) P0 异常 -> HITL 审批挂起 -> 人工通过后恢复状态机（FR-08）

运行：cd supply-chain-agent && python -m sca.demo
"""
from __future__ import annotations

import asyncio
import logging
import time

from sca.agents import SupplyChainGraph, TOOL_REGISTRY
from sca.intent import build_intent_model
from sca.simulation import OrderGenerator, FaultInjector
from sca.mcp.registry import coverage_summary
from sca.eval.harness import run_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sca.demo")


async def scenario_auto_replan():
    log.info("=== 场景 A：P2 异常自动重规划（无审批）===")
    g = SupplyChainGraph(intent_model=build_intent_model())  # 无 Key -> 纯规则，离线可跑
    app = g.compile()

    order = OrderGenerator().next()
    anomaly = FaultInjector().inject("P2")  # 拥堵/天气预警/停留超时 -> 自动换路

    t0 = time.perf_counter()
    result = await app.ainvoke(
        {"order": order, "exception": anomaly},
        config={"configurable": {"thread_id": "demo-A"}},
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0

    log.info("Trace: %s", " -> ".join(result.get("trace", [])))
    log.info("重规划耗时(replan_ms)=%s ms | 全链路耗时=%s ms",
             result.get("metrics", {}).get("replan_ms"), round(wall_ms, 2))
    log.info("计划版本=%s, SOP 命中=%s", result.get("plan_version"), result.get("sop"))


async def scenario_hitl():
    log.info("=== 场景 B：P0 异常 -> HITL 审批挂起 -> 恢复 ===")
    g = SupplyChainGraph(intent_model=build_intent_model())
    app = g.compile(interrupt_before=["approval_gate"])

    order = OrderGenerator().next()
    anomaly = FaultInjector().inject("P0")  # 冷链失效/危险品 -> 必须审批

    from sca._compat import Command, GraphInterrupt

    cfg = {"configurable": {"thread_id": "demo-B"}}
    result = None
    try:
        result = await app.ainvoke(
            {"order": order, "exception": anomaly}, cfg)
    except GraphInterrupt:
        # 可能有多级审批（规划层 + 异常处置层）；循环恢复直至状态机结束
        while True:
            log.info("状态机在 approval_gate 挂起（等待人工审批）...")
            try:
                result = await app.ainvoke(
                    Command(resume={"approved": True, "by": "demo-operator"}), cfg)
                break
            except GraphInterrupt:
                continue
    log.info("审批通过，恢复执行。Trace: %s", " -> ".join(result.get("trace", [])))


async def main():
    await scenario_auto_replan()
    print()
    await scenario_hitl()
    print()
    # ---- 原型期模拟论证（L0/L1）与 MCP 覆盖证据 ----
    cov = coverage_summary()
    log.info("MCP 覆盖：%d 个 Server（核心 %d），覆盖 %d/%d 子系统，缺失=%s",
             cov["servers"], len(cov["core_servers"]), cov["subsystems_total"],
             cov["subsystems_total"], cov["missing"] or "无")
    rep = await run_all()
    passed = sum(1 for m in rep["metrics"] if m.status == "pass")
    failed = sum(1 for m in rep["metrics"] if m.status == "fail")
    na = sum(1 for m in rep["metrics"] if m.status == "na")
    log.info("仿真评估：通过 %d / 失败 %d / 需外部能力验收 %d", passed, failed, na)
    for m in rep["metrics"]:
        log.info("  %s", m)
    log.info("Demo 完成。")


if __name__ == "__main__":
    asyncio.run(main())
