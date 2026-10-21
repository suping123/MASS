"""验证：调度结果须经调度员审核后才向司机/收货方发钉钉（dispatch_review 闸门）。

直接驱动状态图（显式 MemorySaver 检查点，绕开本沙箱 stub 图 + 异步 PG 检查点的既有冲突），
验证 notify 在 dispatch_review 处中断、审核通过才下发、驳回不下发。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sca.agents import SupplyChainGraph
from sca.intent import build_intent_model
from sca._compat import MemorySaver, Command, GraphInterrupt
from sca.simulation import FaultInjector


def build_graph():
    g = SupplyChainGraph(intent_model=build_intent_model())
    return g.compile(checkpointer=MemorySaver())


def base_state(origin, dest, level="P2"):
    return {
        "order": {"order_id": "TEST-DR", "origin": origin, "dest": dest,
                  "cargo": {"type": "普货", "weight_t": 10}, "customer": "测试收货方"},
        "exception": FaultInjector().inject(level),
        "source_text": f"{origin}到{dest} 普货 10吨",
    }


async def run_once(origin, dest, approve):
    graph = build_graph()
    cfg = {"configurable": {"thread_id": "t1"}}
    try:
        res = await graph.ainvoke(base_state(origin, dest), config=cfg)
        intr = (res or {}).get("__interrupt__")
    except GraphInterrupt as e:
        intr = e.payload
    # 官方 langgraph：__interrupt__ 是 Interrupt 对象列表，载荷在 .value；
    # 零依赖垫片：GraphInterrupt.payload 直接是 dict。两种形状都要兼容。
    if isinstance(intr, (list, tuple)) and intr:
        intr = getattr(intr[0], "value", None)
    assert intr and intr.get("type") == "dispatch_review", f"应中断于 dispatch_review，实得 {intr}"
    res2 = await graph.ainvoke(
        Command(resume={"approved": approve, "by": "dispatcher",
                        "reason": None if approve else "复核里程异常"}),
        config=cfg)
    return res2


async def main():
    # 审核通过
    r = await run_once("上海", "北京", True)
    dr = r.get("dispatch_review") or {}
    targets = {n.get("target") for n in (r.get("notifications") or [])}
    print("[通过] status=", dr.get("status"), "targets=", targets)
    assert dr.get("status") in ("approved", "sent_failed")
    assert "司机" in targets and "收货方" in targets

    # 驳回
    r2 = await run_once("广州", "武汉", False)
    dr2 = r2.get("dispatch_review") or {}
    print("[驳回] status=", dr2.get("status"), "reason=", dr2.get("reason"))
    assert dr2.get("status") == "rejected"
    print("\n✅ 调度员审核闸门验证通过")


if __name__ == "__main__":
    asyncio.run(main())
