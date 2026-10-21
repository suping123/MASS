"""S2 并发修复实证：调度期间事件循环有没有被卡住。

**先说清一个容易搞错的结论**（本脚本的第一次尝试就踩了这个坑）：
`plan_prepare -> [agent_route ‖ agent_precheck] -> plan_arbiter` 在拓扑上确实是扇出，
官方 langgraph 也确实会把两支放进同一超步并行执行（最小隔离测试：两个分支 500ms+300ms
墙钟 506ms，串行应为 800ms）。但**观测不到收益**，因为 `agent_precheck` 是纯本地 CPU
工作（本地订单库查询 + 伪向量知识检索，全程没有 await 点），2ms 内跑完都来不及让出
调度权 —— 它总是在 `agent_route` 拿到首个时间片之前就结束了。所以：

  * "两个分支的启动间隔" 恒 ≈ 0，**不能**用来判定并行是否生效（近零时长任务同理，
    "重叠时长" 也恒为 0）。这两个指标都不具判别力。
  * 真正可观测、有价值的收益是：**阻塞调用不再卡死整个事件循环**。

于是本脚本测的是**事件循环最大停顿**：跑一个每 10ms 一跳的心跳协程，同时跑一次真实调度。
如果某处同步阻塞了事件循环 N 毫秒，心跳就会出现一个 ≈N 毫秒的空档。

用法：
  # 修复后（默认）
  .venv/Scripts/python.exe -X utf8 scripts/verify_parallel.py
  # A/B 对照：强制恢复"同步阻塞"旧行为
  PROBE_FORCE_BLOCKING=1 .venv/Scripts/python.exe -X utf8 scripts/verify_parallel.py
"""
import asyncio
import os
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
for p in (str(SCRIPTS), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import verify_p0p1 as V  # noqa: E402  复用场景定义 / 环境注入 / 状态清理

FORCE_BLOCKING = os.environ.get("PROBE_FORCE_BLOCKING", "") not in ("", "0", "false")
TICK_MS = 10.0


def _force_blocking() -> None:
    """把 fetch_bytes 换回"同步阻塞、不让出事件循环"的旧行为，用于 A/B 对照。"""
    import sca.util.aio as aio

    async def _blocking_fetch(req, timeout: float = 8.0) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # 同步阻塞，不 await
            return r.read()

    aio.fetch_bytes = _blocking_fetch


async def _heartbeat(stop: asyncio.Event, stalls: list) -> None:
    """每 TICK_MS 一跳。事件循环被同步调用卡住时，相邻两跳的间隔就是停顿长度。"""
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(TICK_MS / 1000.0)
        now = time.perf_counter()
        stalls.append((now - last) * 1000.0)
        last = now


async def _once(oid: str) -> tuple:
    """跑一次调度，返回 (result, wall_ms, 心跳停顿样本)。"""
    state = dict(V.SCENARIOS["normal"])
    state["order"] = dict(state["order"], order_id=oid)
    V._reset_state([oid, "SO-P0P1-001"])

    stop = asyncio.Event()
    stalls: list = []
    hb = asyncio.create_task(_heartbeat(stop, stalls))

    t0 = time.perf_counter()
    result, _ = await V._run("normal", state)
    wall = (time.perf_counter() - t0) * 1000.0

    stop.set()
    await hb
    V._reset_state([oid, "SO-P0P1-001"])
    # 去掉首跳（起跳时刻不准）与末尾（stop 引起的尾跳）
    return result, wall, (stalls[1:-1] if len(stalls) > 2 else stalls)


async def main() -> int:
    V._block_external_notify()
    if FORCE_BLOCKING:
        _force_blocking()

    from sca._compat import USING_REAL_LANGGRAPH
    print(f"[s2] 引擎={'官方 langgraph' if USING_REAL_LANGGRAPH else '零依赖垫片'}  "
          f"阻塞处理={'同步阻塞（旧行为）' if FORCE_BLOCKING else '线程池（S2 修复后）'}")

    # ⚠️ 必须预热：首轮会带上「惰性 import + china_cities.json(2135 条) 加载 + 首次建图」
    # 的一次性成本，实测会造成 ~1580ms 的停顿，且只在第一轮出现（第 2/3 轮降到 ~170ms）。
    # 不预热就会把这个一次性开销误判成"每次调度都阻塞"。
    print("[s2] 预热一轮（排除首次 import / 建图的一次性成本，不计入结论）…")
    await _once("SO-PAR-WARM")

    result, wall, samples = await _once("SO-PAR-STALL")
    worst = max(samples) if samples else 0.0
    over = [s for s in samples if s > TICK_MS * 5]

    br = {}
    for row in (result or {}).get("agent_log") or []:
        if isinstance(row, dict) and row.get("agent") in ("agent_route", "agent_precheck"):
            br[row["agent"]] = row
    rt, pc = br.get("agent_route"), br.get("agent_precheck")

    print(f"[s2] 整轮墙钟 {wall:.0f}ms ；agent_route "
          f"{rt['ms'] if rt else '?'}ms ；agent_precheck {pc['ms'] if pc else '?'}ms")
    print(f"[s2] 心跳样本 {len(samples)} 个（间隔应 ≈{TICK_MS:.0f}ms）"
          f"，最大停顿 {worst:.1f}ms，超过 5×tick 的次数 {len(over)}")
    if over:
        print(f"[s2] 停顿明细(ms)：{[round(x, 1) for x in sorted(over, reverse=True)[:6]]}")

    if FORCE_BLOCKING:
        if worst > 300:
            print(f"[s2] PASS(对照组) 旧行为下事件循环被卡住 {worst:.0f}ms —— 修复目标已复现")
            return 0
        print(f"[s2] FAIL(对照组) 期望出现 >300ms 停顿，实测最大 {worst:.1f}ms")
        return 1
    if worst < 250:
        print(f"[s2] PASS 单次调度最大停顿 {worst:.1f}ms（对照组为数百毫秒级）—— "
              f"事件循环保持可调度，S2 修复生效")
        print("[s2] 说明：剩余 ~170ms 停顿是纯 CPU 工作（县级地图匹配 / 容量打分 / JSON 解析），"
              "属独立优化项；用 to_thread 包 CPU 工作在 GIL 下收益有限。")
        return 0
    print(f"[s2] FAIL 最大停顿 {worst:.1f}ms，仍存在同步阻塞点")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
