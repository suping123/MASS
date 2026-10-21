"""P0/P1 改造验证脚本：跑固定场景，输出可对比的状态快照。

用法：
  # 保存基线（改造前）——必须带 --deterministic，否则外部抖动会污染基线
  .venv/Scripts/python.exe -u scripts/verify_p0p1.py --deterministic \
      --save results/_p0_baseline_det.json
  # 改造后对比（逐字段 diff）
  .venv/Scripts/python.exe -u scripts/verify_p0p1.py --deterministic \
      --allow-trace-diff --compare results/_p0_baseline_det.json
  # 另跑一遍零依赖垫片路径，确认两引擎结果不漂移
  .venv/Scripts/python.exe -u scripts/verify_p0p1.py --shim --deterministic \
      --allow-trace-diff --compare results/_p0_baseline_det.json

为什么必须 --deterministic（2026-10-16 实测教训）：
  不加时同一份代码连续两次跑，exception 场景能差 17 处、normal/p1 各差 1 处 —— 根因
  全在**外部**，与改造无关：
    ① 高德实时路况选路：同一 OD 不同时刻 884.8 / 884.9（provider 同为 amap，
       途经点同为 25 个），0.1km 会顺着 est_cost / eta / notes 放大成十几处差异；
    ② 路线裁决模型：exception 原话含「高速」→ segment_hint 命中 → allow_model=True
       → 模型每次可能给 accept / 改道两种提案，reroute / detour_km / cost / eta
       整体翻转。
  关掉它们后同一份代码自比 **0 处差异**，快照可复现，比对才可信。

安全约定（脚本自行注入，不依赖调用方）：
  * 外部通知**被强制拦截** —— notify_server._config() 优先读 data/credentials.json 的
    「工具 Key 清单」，优先级高于环境变量；仅设 SCA_DINGTALK_WEBHOOK 空不足以阻断，
    故此处直接 patch 掉凭据读取 + 关闭桌面弹窗。拦截后走 audit_only：只写审计日志，
    不推送钉钉、不弹本机窗口。
  * SCA_USE_PG=false     —— 不起 PG，纯内存检查点（PG 未启动时 h_* 会挂住进程）
  * SCA_MEMORY_ENABLED=false —— 不探测 Redis，避免无关延迟
  * SCA_DISPATCH_REVIEW_ENABLED=false —— 跳过下发审核挂起（否则每单都要人工 resume）
  * SCA_GPS_AUTO_ADVANCE=0 —— 司机位置默认停在起运地，保证确定性
  * 每个场景跑前清理该订单在 tms/gps 的残留，保证多次运行可复现（不动其它数据）

对比口径：
  * trace 序列必须逐项一致（P0 要求）；P1 会新增节点名，用 --allow-trace-diff 放宽
  * mcp_calls 用排序后的「server.tool:ok」集合对比（并行后顺序不保证）
  * current_plan / route / cost / eta / capacity / execution 必须一致
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# —— 必须在 import sca 之前注入（settings 是模块级单例，import 时即读 .env）——
os.environ["SCA_USE_PG"] = "false"
os.environ["SCA_DINGTALK_WEBHOOK"] = ""
os.environ["SCA_MEMORY_ENABLED"] = "false"
os.environ["SCA_DISPATCH_REVIEW_ENABLED"] = "false"
os.environ["SCA_GPS_AUTO_ADVANCE"] = "0"
os.environ["SCA_NOTIFY_DESKTOP"] = "false"   # 不弹本机桌面通知

if sys.platform == "win32":  # psycopg / 异步驱动在 Proactor 下不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 固定场景（起讫明确、货类明确；不含路线偏好词 -> 初始计划走确定性 plan_route，
# 不会被模型增强版引入非确定性）。三条主路径各覆盖一个：
#   normal    —— 无异常 -> detect_exception 直连 notify（跳过处置链）
#   exception —— P2 停留超时 -> 完整处置链 + 应急重规划
#   p1        —— 时效违约 P1 -> approval_gate 高危审批挂起 -> 审批后转重规划
# ---------------------------------------------------------------------------
_ORDER = {
    "order_id": "SO-P0P1-001",
    "origin": "石家庄",
    "dest": "南京",
    "route_hint": "石家庄→南京",
    "cargo": {"type": "普货", "weight_t": 8.0},
    "contact": "13800000000",
}

SCENARIOS = {
    "normal": {
        "order": dict(_ORDER, required_by="3天内送达"),
        "source_text": "石家庄到南京 普货 8吨，3天内送达",
    },
    "exception": {
        "order": dict(_ORDER, order_id="SO-P0P1-002",
                      required_by="3天内送达"),
        "source_text": "石家庄到南京 普货 8吨，车辆在高速上停留超过两小时未动",
        "exception": {
            "code": "E-TMS-STOP", "name": "停留超时", "level": "P2",
            "desc": "车辆在高速上停留超过两小时未动", "source": "injected",
        },
    },
    "p1": {
        "order": dict(_ORDER, order_id="SO-P0P1-003",
                      required_by="今天下午6点前送达"),
        "source_text": "石家庄到南京 普货 8吨，今天下午6点前必须送达",
    },
}


def _block_external_notify() -> None:
    """阻断真实钉钉推送（见模块 docstring 的说明）。"""
    import sca.mcp.notify_server as ns
    ns._config = lambda: ("", "")   # 无凭据 -> send_dispatch_notice 走 audit_only


def _reset_state(order_ids) -> None:
    """清理测试订单在 tms/gps 的残留（内存 + 磁盘），保证多次运行结果可复现。

    只删磁盘不够：tms_server/gps_server 的 _ORDERS/_STATE 是模块级全局，import 时
    一次性载入，之后的任何写操作都会把内存里的完整字典回写磁盘，从而撤销外部删除
    （表现为"已存在（status=...），拒绝重复下发"这类跨运行的脏状态）。
    """
    from sca.mcp import gps_server, tms_server

    for mod, attr, fn in ((tms_server, "_ORDERS", "tms_orders.json"),
                          (gps_server, "_STATE", "gps_state.json")):
        mem = getattr(mod, attr, None)
        if isinstance(mem, dict):
            for k in [k for k in list(mem) if any(o in str(k) for o in order_ids)]:
                mem.pop(k, None)
        p = ROOT / "data" / fn
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        dropped = [k for k in d if any(oid in str(k) for oid in order_ids)]
        for k in dropped:
            d.pop(k, None)
        if dropped:
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


async def _run(name: str, state_in: dict) -> tuple:
    from sca._compat import Command, GraphInterrupt, MemorySaver
    from sca.agents import SupplyChainGraph

    g = SupplyChainGraph()
    graph = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": f"verify-{name}"}}
    resume = Command(resume={"approved": True, "by": "verify_script"})

    t0 = time.perf_counter()
    inp = dict(state_in)
    result: Any = None
    for _ in range(6):   # 兜底：approval_gate 遇 P0/P1 会挂起，自动放行以免脚本卡住
        try:
            result = await graph.ainvoke(inp, config=cfg)
        except GraphInterrupt:      # 零依赖垫片：中断以异常形式抛出
            inp = resume
            continue
        if isinstance(result, dict) and result.get("__interrupt__"):
            inp = resume            # 官方 langgraph：中断以 __interrupt__ 返回
            continue
        break
    return result, round((time.perf_counter() - t0) * 1000, 1)


def _ids(rows) -> list:
    out = []
    for x in rows or []:
        if isinstance(x, dict):
            out.append(str(x.get("id") or x.get("name") or ""))
    return out


def snapshot(result: dict, elapsed_ms: float = 0.0) -> dict:
    calls = result.get("mcp_calls") or []
    return {
        "trace": list(result.get("trace") or []),
        "trace_len": len(result.get("trace") or []),
        "elapsed_ms": elapsed_ms,
        "mcp_calls": [{"server": c.get("server"), "tool": c.get("tool"),
                       "ok": c.get("ok")} for c in calls],
        "mcp_calls_set": sorted(
            {f"{c.get('server')}.{c.get('tool')}:{c.get('ok')}" for c in calls}),
        "order": {k: (result.get("order") or {}).get(k)
                  for k in ("order_id", "origin", "dest", "status")},
        "capacity_ids": _ids(result.get("capacity")),
        "current_plan": result.get("current_plan"),
        "route": result.get("route"),
        "cost": result.get("cost"),
        "eta": result.get("eta"),
        "execution": result.get("execution"),
        "exception": {k: (result.get("exception") or {}).get(k)
                      for k in ("code", "name", "level", "source")},
        "sop_id": (result.get("sop") or {}).get("sop_id"),
        "route_decision": result.get("route_decision"),
        "plan_version": result.get("plan_version"),
        "metrics": result.get("metrics"),
        "agent_log": result.get("agent_log") or [],
        "notifications": [{"channel": n.get("channel"), "target": n.get("target"),
                           "sent": n.get("sent")}
                          for n in (result.get("notifications") or [])],
        "dispatch_review_status": (result.get("dispatch_review") or {}).get("status"),
    }


# 非确定性字段：模型生成的文本、实测耗时、运行时刻产生的时间戳，逐次运行必然不同，
# 与改造无关。对比时豁免，但会在输出里显式报告，避免掩盖真问题。
# 刻意**不**豁免 notes / skip_reason / execution.reason / order.status —— 它们是确定性
# 结果，一旦出现差异就是真问题（例如"运单已存在"说明有跨运行脏状态）。
_NONDET_SEGMENTS = {"model_decision", "summary", "elapsed_ms", "replan_ms",
                    "created_at", "status_history", "ts",
                    # agent_log 是纯耗时观测（各 Agent 自报 t_start/t_end/ms）：
                    # 只豁免这三个计时键，其余键（agent/calls/ok/degraded/
                    # distance_km/carrier/order_exists/compliance_hits/skipped）
                    # 仍参与比对，避免把"Agent 实际行为变了"一起放过。
                    "t_start", "t_end", "ms"}
_NONDET_SUFFIXES = ("execution.tms_receipt.eta",)   # ISO 时间串，随下发时刻变化


def _is_nondet(path: str) -> bool:
    if path.endswith(_NONDET_SUFFIXES):
        return True
    return any(seg in _NONDET_SEGMENTS for seg in path.split("."))


def _strip_timings(rows):
    """剥掉 agent_log 各条目的计时键（t_start/t_end/ms），保留其余观测字段。

    `_diff` 对列表是**整体比对、不递归**，所以子路径豁免不生效——必须在这里
    先去噪，才能既忽略耗时抖动、又抓住"Agent 实际行为变了"（calls/ok/degraded/
    order_exists/compliance_hits/carrier/skipped 仍在比对范围内）。
    """
    if not isinstance(rows, list):
        return rows
    return [({k: v for k, v in r.items() if k not in ("t_start", "t_end", "ms")}
             if isinstance(r, dict) else r) for r in rows]


def _diff(a, b, path: str, out: list) -> None:
    if _is_nondet(path):
        return
    if path.endswith("agent_log"):
        a, b = _strip_timings(a), _strip_timings(b)
    if type(a) is not type(b) and not (isinstance(a, (int, float))
                                       and isinstance(b, (int, float))):
        out.append(f"{path}: 类型不同 {type(a).__name__} -> {type(b).__name__}")
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: 新增 {json.dumps(b[k], ensure_ascii=False)[:120]}")
            elif k not in b:
                out.append(f"{path}.{k}: 缺失（原为 {json.dumps(a[k], ensure_ascii=False)[:120]}）")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list):
        if a != b:
            out.append(f"{path}: 列表不同\n      原: {json.dumps(a, ensure_ascii=False)[:300]}"
                       f"\n      新: {json.dumps(b, ensure_ascii=False)[:300]}")
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")


async def _run_all() -> dict:
    out = {}
    for name, st in SCENARIOS.items():
        _reset_state([st["order"]["order_id"]])
        res, ms = await _run(name, st)
        out[name] = snapshot(res, ms)
        print(f"[verify] 场景 {name}: trace={out[name]['trace_len']} 节点, "
              f"mcp_calls={len(out[name]['mcp_calls'])} 次, 耗时 {ms}ms")
    await asyncio.sleep(0.3)   # 给模型客户端连接池留出清理时间，避免关闭告警
    return out


def _force_deterministic() -> str:
    """关闭两个**外部不确定性源**，让快照可复现。

    为什么必须关：否则无法区分「P1 回归」与「外部抖动」，比对结果不可信。实测（2026-10-16）：
      ① **高德实时路况选路**——同一 OD（石家庄→南京）不同时刻返回 884.8 / 884.9，
         provider 同为 amap、途经点同为 25 个，仅里程差 0.1km；该 0.1km 会顺着
         est_cost / notes / eta 放大成十几处差异。
      ② **路线裁决模型**——exception 场景原话含「高速」，`segment_hint` 命中 →
         `allow_model=True` → 模型每次可能给 accept / 改道两种提案，导致
         reroute / detour_km / cost / eta 整体翻转（实测同代码两次跑差 17 处）。
    关法：高德取 Key 的函数返回空串（走内置路网几何）；模型两个底层网络函数打桩抛错
    （走 route_llm 既有的 fail-open 路径，等价"模型不可用"）。
    """
    import sca.mcp.map_server as ms
    import sca.mcp.route_llm as rl

    ms._amap_key = lambda: ""          # ① 高德关闭 -> 内置路网几何（完全确定）

    def _boom(*_a, **_k):              # ② 模型通道关闭 -> fail-open
        raise RuntimeError("verify: 模型通道已在确定性模式下关闭")

    rl._chat = _boom
    rl._chat_with_tools = _boom
    return "已关闭高德实时选路 + 路线模型（快照可复现）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", help="保存快照到该路径（作为基线）")
    ap.add_argument("--compare", help="与该基线文件逐字段对比")
    ap.add_argument("--allow-trace-diff", action="store_true",
                    help="允许 trace 序列不同（P1 起新增节点名时使用）")
    ap.add_argument("--shim", action="store_true",
                    help="屏蔽 langgraph，强制用零依赖垫片跑（验证两条路径行为不漂移）")
    ap.add_argument("--deterministic", action="store_true",
                    help="关闭高德实时选路与路线模型，消除外部抖动（推荐用于等价性比对）")
    args = ap.parse_args()

    if args.shim:
        import importlib.abc

        class _BlockLangGraph(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "langgraph" or fullname.startswith("langgraph."):
                    raise ImportError("blocked for shim verification")
                return None

        sys.meta_path.insert(0, _BlockLangGraph())
        print("[verify] 已屏蔽 langgraph，强制走零依赖垫片")

    _block_external_notify()
    from sca._compat import USING_REAL_LANGGRAPH
    print("[verify] 引擎：", "官方 langgraph" if USING_REAL_LANGGRAPH else "零依赖垫片（串行）")
    print("[verify] 外部通知已拦截（不推钉钉、不弹窗，仅审计留痕）")
    if args.deterministic:
        print("[verify]", _force_deterministic())

    snaps = asyncio.run(_run_all())

    if args.save:
        p = Path(args.save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snaps, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[verify] 基线已保存：{p}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        total = 0
        for name in SCENARIOS:
            diffs: list = []
            b, n = base.get(name, {}), snaps[name]
            skip = {"trace", "trace_len", "elapsed_ms"} if args.allow_trace_diff else set()
            for field in b:
                if field in skip:
                    continue
                if field not in n:
                    diffs.append(f"{field}: 新快照缺少该字段")
                    continue
                _diff(b[field], n[field], field, diffs)
            status = "一致" if not diffs else f"{len(diffs)} 处差异"
            print(f"\n[compare] 场景 {name}: {status}")
            if args.allow_trace_diff:
                print(f"   （已放宽 trace/耗时对比）新 trace: {n['trace']}")
            for d in diffs[:40]:
                print("   -", d)
            if len(diffs) > 40:
                print(f"   ... 另有 {len(diffs) - 40} 处")
            total += len(diffs)
        print(f"\n[compare] 合计差异 {total} 处")
        return 1 if total else 0

    if not args.save and not args.compare:
        print(json.dumps(snaps, ensure_ascii=False, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
