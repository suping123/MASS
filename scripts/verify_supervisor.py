"""S3（Supervisor 竖切片）验证脚本。

三层验证，缺一层都不够：
  A. **纯函数层** —— `eligible()` / `decide()` / `legacy_route()` / `validate_targets()`
     逐条断言，并把"同批不得含两个 interrupt 节点"这条硬约束显式测掉。
  B. **三模式图级等价** —— `off`（改造前基线）/ `shadow` / `on` 各跑同一批场景，比对
     业务结果快照。判据：`shadow` 必须与 `off` **逐字段一致**（影子模式的定义就是
     "路由不变"），`on` 必须能跑完且不出现"规划段停滞"。
  C. **故障注入（不变量与不死锁）** —— 把 `plan_arbiter` 的校验器打桩成恒定缺陷，
     验证：第 1 次重派 -> 第 2 次转人工 -> `approval_gate` 承接 -> 放行才下发 /
     驳回不下发且整单在 `end` 收口。这一层证明"做完对不对"是真在拦人，而不是摆样子。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_supervisor.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

for _k in ("SCA_USE_PG",):
    os.environ.setdefault(_k, "false")

if sys.platform == "win32":  # psycopg / 异步驱动在 Proactor 下不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESULT_MARK = "__SUPERVISOR_RESULT__"

_MODES = ("off", "shadow", "on")


# ===========================================================================
# A. 纯函数层断言（不需要 settings / 图 / 外部服务）
# ===========================================================================
def _snap(**kw):
    """构造一个"处于某阶段"的 state 快照。"""
    base = {}
    base.update(kw)
    return base


_CTX = {"origin": "石家庄", "dest": "南京", "cargo_type": "普货"}
_ROUTE = {"waypoints": ["石家庄", "南京"], "distance_km": 884.9, "origin": "石家庄",
          "dest": "南京"}
_PRE = {"order_check": {"exists": False}, "degraded": False, "notes": []}
_PLAN = {"route": "石家庄→南京", "waypoints": ["石家庄", "南京"], "distance_km": 884.9,
         "carriers": [{"id": "C1"}], "carrier": {"id": "C1"}, "version": 1,
         "degraded": False}

PHASES = {
    "空 state": _snap(),
    "仅 plan_context": _snap(plan_context=_CTX),
    "route 就绪": _snap(plan_context=_CTX, route=_ROUTE),
    "两分支就绪": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE),
    "plan 就绪(常规)": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE,
                             current_plan=_PLAN, plan_arbitration={"agents": []},
                             evaluation={"accepted": {"plan_arbiter": True}}),
    "plan 就绪(P1 未审批)": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE,
                                  current_plan=_PLAN, plan_arbitration={"agents": []},
                                  exception={"level": "P1"},
                                  evaluation={"accepted": {"plan_arbiter": True}}),
    "plan 就绪(P1 已审批)": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE,
                                  current_plan=_PLAN, plan_arbitration={"agents": []},
                                  exception={"level": "P1"}, approval={"approved": True},
                                  evaluation={"accepted": {"plan_arbiter": True}}),
    "校验未过(待重试)": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE,
                              current_plan=_PLAN, plan_arbitration={"agents": []},
                              evaluation={"accepted": {}, "flagged": {"plan_arbiter": ["x"]}}),
    "校验未过(转人工)": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE,
                              current_plan=_PLAN, plan_arbitration={"agents": []},
                              evaluation={"accepted": {}, "needs_human": {"plan_arbiter": ["x"]}}),
    "已下发": _snap(plan_context=_CTX, route=_ROUTE, precheck=_PRE, current_plan=_PLAN,
                    plan_arbitration={"agents": []}, execution={"ok": True},
                    evaluation={"accepted": {"plan_arbiter": True}}),
}

_INTERRUPTS = {"approval_gate", "notify"}   # 图里仅有的两个挂起点


def part_a() -> list:
    """返回失败信息列表（空 = 全通过）。"""
    from sca.supervisor import (decide, eligible, gate_passed, handoff_target,
                                legacy_route, validate_targets)
    from sca.supervisor.decide import decide_by_rule
    from sca.supervisor.precondition import MAX_RETRY  # noqa: F401

    fails: list = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # ---- A1 eligible 表：各阶段该出现哪些动作 ----
    expect = {
        "空 state": {"plan_prepare"},
        "仅 plan_context": {"agent_route", "agent_precheck"},
        "route 就绪": {"agent_precheck"},
        "两分支就绪": {"plan_arbiter"},
        "plan 就绪(常规)": {"execute"},
        "plan 就绪(P1 未审批)": {"approval_gate"},
        "plan 就绪(P1 已审批)": {"execute"},
        "校验未过(待重试)": {"plan_arbiter"},
        "校验未过(转人工)": {"approval_gate"},
        "已下发": set(),
    }
    for name, st in PHASES.items():
        got = set(eligible(st))
        chk(got == expect[name], f"A1 eligible[{name}] 期望 {sorted(expect[name])} 实得 {sorted(got)}")

    # ---- A2 硬约束：同批不得同时含两个 interrupt 节点 ----
    # 两个挂起点若进同一批，LangGraph 只保留 `intr[0]`（`_interrupt_payload` 的口径），
    # 另一个载荷会静默丢失。这里对每个阶段都验证一次。
    for name, st in PHASES.items():
        acts = set(decide(st).next_actions)
        chk(len(acts & _INTERRUPTS) <= 1,
            f"A2 [{name}] 同批出现多个 interrupt 节点：{sorted(acts & _INTERRUPTS)}")

    # ---- A3 业务不变量：高危未审批 & 校验未过，都不得下发 ----
    chk("execute" not in eligible(PHASES["plan 就绪(P1 未审批)"]),
        "A3 高危未审批却允许 execute（绕过人工闸口）")
    chk("execute" not in eligible(PHASES["校验未过(转人工)"]),
        "A3 校验未过却允许 execute（校验形同虚设）")

    # ---- A4 decide() 的四条分支 ----
    d = decide_by_rule(PHASES["空 state"])
    chk(d.next_actions == ["plan_prepare"] and d.rule == "sequential",
        f"A4 空 state 应单步派 plan_prepare，实得 {d.next_actions}/{d.rule}")
    d = decide_by_rule(PHASES["仅 plan_context"])
    chk(sorted(d.next_actions) == ["agent_precheck", "agent_route"] and d.rule == "fanout_parallel",
        f"A4 两分支就绪应并行扇出，实得 {d.next_actions}/{d.rule}")
    d = decide_by_rule(PHASES["已下发"])
    chk(d.next_actions == [] and d.rule == "handoff",
        f"A4 已下发应 handoff，实得 {d.next_actions}/{d.rule}")
    chk(decide(PHASES["空 state"], max_rounds=0).rule == "max_rounds_exceeded",
        "A4 轮次耗尽未触发强制交回")

    # ---- A5 validate_targets 必须挡住非法目标（官方引擎会静默忽略未知目标）----
    chk(validate_targets(["agent_route"]) == [], "A5 合法目标被误判")
    chk(validate_targets(["monitor", "end"]) == [], "A5 交回落点被误判为非法")
    chk(validate_targets(["nope"]) == ["nope"], "A5 非法目标未被挡住")

    # ---- A6 legacy_route 复刻原拓扑 ----
    leg = {
        "空 state": ["plan_prepare"],
        "仅 plan_context": ["agent_route", "agent_precheck"],
        "route 就绪": ["agent_precheck"],
        "两分支就绪": ["plan_arbiter"],
        "plan 就绪(常规)": ["execute"],
        "plan 就绪(P1 未审批)": ["approval_gate"],
        "plan 就绪(P1 已审批)": ["execute"],
        "已下发": ["monitor"],
    }
    for name, want in leg.items():
        got = legacy_route(PHASES[name])
        chk(sorted(got) == sorted(want), f"A6 legacy_route[{name}] 期望 {want} 实得 {got}")

    # ---- A7 影子模式的"不一致"必须可观测：校验缺陷会让决策与原拓扑分叉 ----
    st = PHASES["校验未过(待重试)"]
    chk(decide(st).next_actions == ["plan_arbiter"],
        "A7 待重试时应重派 plan_arbiter")
    # 原拓扑不认识"校验"这件事：plan 一旦产出就直奔 execute。
    # 因此这里必然分叉 —— 正好证明影子模式能测出"新逻辑与原逻辑不一致"。
    chk(legacy_route(st) == ["execute"],
        "A7 原拓扑在 plan 已产出时应直奔 execute")
    chk(decide(st).next_actions != legacy_route(st),
        "A7 该场景下决策与原拓扑应当不一致（否则测不出影子模式的比对能力）")

    # ---- A8 交回落点 ----
    chk(handoff_target(PHASES["已下发"]) == "monitor", "A8 已下发应交回 monitor")
    chk(handoff_target(PHASES["校验未过(转人工)"]) == "approval_gate",
        "A8 校验转人工应落到 approval_gate")
    chk(handoff_target(PHASES["空 state"]) == "end", "A8 停滞应落 end（而不是静默结束）")

    # ---- A9 闸口判定（2026-10-17 收口的"驳回不得下发"）----
    chk(gate_passed({"approved": True}), "A9 审批通过被判为未放行")
    chk(gate_passed({"auto": True}), "A9 无需审批（auto）被判为未放行")
    chk(not gate_passed({"approved": False}), "A9 驳回被判为放行（松口回归）")
    chk(not gate_passed({"approved": False, "escalation_resolved": True}),
        "A9 升级驳回被判为放行（松口回归）")
    chk(not gate_passed({}), "A9 无审批记录被判为放行（应 fail-closed）")
    chk(not gate_passed(None), "A9 None 被判为放行（应 fail-closed）")
    return fails


# ===========================================================================
# B / C. 图级运行
# ===========================================================================
_ORDER = {
    "order_id": "SO-SUP-001",
    "origin": "石家庄",
    "dest": "南京",
    "route_hint": "石家庄→南京",
    "cargo": {"type": "普货", "weight_t": 8.0},
    "contact": "13800000000",
}

SCENARIOS = {
    "normal": {"order": dict(_ORDER, required_by="3天内送达"),
               "source_text": "石家庄到南京 普货 8吨，3天内送达"},
    "exception": {"order": dict(_ORDER, order_id="SO-SUP-002", required_by="3天内送达"),
                  "source_text": "石家庄到南京 普货 8吨，车辆在高速上停留超过两小时未动",
                  "exception": {"code": "E-TMS-STOP", "name": "停留超时", "level": "P2",
                                "desc": "车辆在高速上停留超过两小时未动",
                                "source": "injected"}},
    "p1": {"order": dict(_ORDER, order_id="SO-SUP-003", required_by="今天下午6点前送达"),
           "source_text": "石家庄到南京 普货 8吨，今天下午6点必须送达"},
}


def _block_external_notify() -> None:
    """阻断真实钉钉推送（与 verify_p0p1 同口径）。"""
    import sca.mcp.notify_server as ns
    ns._config = lambda: ("", "")


def _reset_state(order_ids) -> None:
    """清理测试订单在 tms/gps 的内存 + 磁盘残留，保证多次运行可复现。"""
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


async def _drive(graph, state_in: dict, thread_id: str, approvals=None) -> dict:
    """驱动一张图跑到底：遇 interrupt 按 `approvals` 顺序自动放行（默认全部批准）。"""
    from sca._compat import Command, GraphInterrupt

    approvals = list(approvals or [])
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}
    inp: object = dict(state_in)
    result: object = None
    for _ in range(8):   # 最多 2 个挂起点 + 余量
        try:
            result = await graph.ainvoke(inp, config=cfg)
        except GraphInterrupt:                      # 零依赖垫片：异常形式
            vals = approvals.pop(0) if approvals else {"approved": True}
            inp = Command(resume=vals)
            continue
        if isinstance(result, dict) and result.get("__interrupt__"):
            vals = approvals.pop(0) if approvals else {"approved": True}
            inp = Command(resume=vals)
            continue
        break
    return result if isinstance(result, dict) else {}


BUSINESS_FIELDS = ("current_plan", "route", "cost", "eta", "execution",
                   "plan_version", "exception", "sop_id", "route_decision")


def snapshot(res: dict) -> dict:
    """业务结果快照（只取确定性字段）。

    刻意剔除两类"与改造无关"的逐次变化量，否则比对只会被噪声淹没：
      * `exception.ts` —— 规则引擎命中时刻的 wall clock；
      * `execution.tms_receipt` 里的 `eta` / `created_at` / `status_history` ——
        `now + eta_h` 与落盘时刻算出的时间串（与 verify_p0p1 的豁免口径一致）。
    其余字段（含 `degraded_reason` / `order_status` / `notifications`）一律参与比对，
    它们的差异一定是真问题。
    """
    out = {f: res.get(f) for f in BUSINESS_FIELDS}
    exc = res.get("exception") or {}
    out["exception"] = {k: exc.get(k) for k in ("code", "name", "level", "source")}
    ex = dict(res.get("execution") or {})
    if isinstance(ex.get("tms_receipt"), dict):
        ex["tms_receipt"] = {k: v for k, v in ex["tms_receipt"].items()
                             if k not in ("eta", "created_at", "status_history")}
    out["execution"] = ex
    out["order_status"] = (res.get("order") or {}).get("status")
    out["capacity_ids"] = [str((c or {}).get("id")) for c in (res.get("capacity") or [])]
    out["dispatch_review_status"] = (res.get("dispatch_review") or {}).get("status")
    out["notifications"] = [(n or {}).get("target") for n in (res.get("notifications") or [])]
    return out


async def _run_scenarios(mode: str, status: dict, approvals=None) -> dict:
    from sca._compat import MemorySaver
    from sca.agents import SupplyChainGraph

    out = {}
    for name, st in SCENARIOS.items():
        _reset_state([st["order"]["order_id"]])
        g = SupplyChainGraph()
        graph = g.compile(checkpointer=MemorySaver())
        t0 = time.perf_counter()
        res = await _drive(graph, st, f"sup-{mode}-{name}", approvals=approvals)
        out[name] = snapshot(res)
        tr = list(res.get("supervisor_trace") or [])
        out[name]["_sup"] = {
            "rounds": len(tr),
            "decisions": [x.get("decision", {}).get("next_actions") for x in tr],
            "gotos": [x.get("goto") for x in tr],
            "match": [x.get("match") for x in tr],
            "modes": sorted({x.get("mode") for x in tr}),
            "stalled": any(x.get("goto") == ["end"] for x in tr),
            "evaluated": (res.get("evaluation") or {}).get("verdicts"),
            "needs_human": list(((res.get("evaluation") or {}).get("needs_human") or {})),
        }
        print(f"[verify] {mode:6s} 场景 {name:10s} 节点={len(res.get('trace') or []):3d} "
              f"supervisor轮次={out[name]['_sup']['rounds']:2d} "
              f"耗时={round((time.perf_counter() - t0) * 1000)}ms", flush=True)
    await asyncio.sleep(0.3)
    return out


async def _run_defect_injection(status: dict) -> dict:
    """C 层：注入恒定缺陷，验证"重试 -> 转人工 -> 闸口闭环 -> 拦下/放行"。"""
    # ⚠️ 必须用 importlib 取**子模块**：`sca/supervisor/__init__.py` 里的
    # `from .evaluate import evaluate` 会把包属性 `evaluate` 重绑成函数，
    # `import sca.supervisor.evaluate as EV` 于是拿到函数而不是模块。
    import importlib
    EV = importlib.import_module("sca.supervisor.evaluate")
    from sca._compat import MemorySaver
    from sca.agents import SupplyChainGraph

    real = dict(EV.CHECKS)
    EV.CHECKS["plan_arbiter"] = lambda s: ["注入缺陷：用于验证校验闸口"]
    out = {}
    try:
        for label, approvals in (("放行", [{"approved": True, "by": "verify"}]),
                                 ("驳回", [{"approved": False, "by": "verify"}])):
            st = SCENARIOS["normal"]
            _reset_state([st["order"]["order_id"]])
            graph = SupplyChainGraph().compile(checkpointer=MemorySaver())
            res = await _drive(graph, st, f"sup-defect-{label}", approvals=approvals)
            ev = res.get("evaluation") or {}
            tr = list(res.get("supervisor_trace") or [])
            out[label] = {
                "attempts": ev.get("attempts"),
                "needs_human": list(ev.get("needs_human") or {}),
                "approval": res.get("approval"),
                "execution_ok": (res.get("execution") or {}).get("ok"),
                "order_status": (res.get("order") or {}).get("status"),
                "gotos": [x.get("goto") for x in tr],
                "trace_tail": list(res.get("trace") or [])[-4:],
                "generated_shipment": bool((res.get("execution") or {}).get("tms_receipt")),
            }
            print(f"[verify] C 注入缺陷/人工{label}: 重派次数={out[label]['attempts']} "
                  f"转人工={out[label]['needs_human']} 下发={out[label]['execution_ok']} "
                  f"运单状态={out[label]['order_status']}", flush=True)
    finally:
        EV.CHECKS.clear()
        EV.CHECKS.update(real)
    return out


# ===========================================================================
# 主流程
# ===========================================================================
async def _run_gate_reject(mode: str) -> dict:
    """D 层：人工在审批闸口**驳回** —— 不许下发、不许重复下发，且必须收口。

    这是 2026-10-17 修掉的一处松口。改造前 `off` 模式 `approval_gate` 的出边是静态
    `-> execute`，驳回与批准走同一条路；`on` 模式因 supervisor 要求 `approved` 才收口到
    `end`。同一件事两种结论。

    两种驳回必须分开测，因为闸口有两个入口：
      * `plan`  —— 规划段高危审批（异常在规划前就已知）。驳回后**一次都不许下发**；
      * `disposal` —— 处置段二次审批（P1 是由执行段 SLA 规则才判出来的，运单已经在途）。
                     驳回后**不许重复下发**，但已发生的下发不能被抹掉。
    """
    from sca._compat import MemorySaver
    from sca.agents import SupplyChainGraph

    # ① 规划段：异常在触发时就已定为 P1（注入），审批发生在 execute 之前
    plan_st = {
        "order": dict(_ORDER, order_id="SO-SUP-004", required_by="今天下午6点前送达"),
        "source_text": "石家庄到南京 普货 8吨，前方交通事故封路，今天下午6点前必须送达",
        "exception": {"code": "E-ACCIDENT", "name": "交通事故封路", "level": "P1",
                      "desc": "前方交通事故封路", "source": "injected"},
    }
    # ② 处置段：p1 的 P1 是执行段 SLA 规则判出来的，运单已下发，闸口在处置段
    disp_st = SCENARIOS["p1"]

    out = {}
    for label, st in (("plan", plan_st), ("disposal", disp_st)):
        _reset_state([st["order"]["order_id"]])
        graph = SupplyChainGraph().compile(checkpointer=MemorySaver())
        res = await _drive(graph, st, f"sup-reject-{mode}-{label}",
                           approvals=[{"approved": False, "by": "verify"}])
        tr = list(res.get("trace") or [])
        out[label] = {
            "order_status": (res.get("order") or {}).get("status"),
            "approval": res.get("approval"),
            "generated_shipment": bool((res.get("execution") or {}).get("tms_receipt")),
            "execute_times": tr.count("execute"),
            "has_notify": "notify" in tr,
            "trace_tail": tr[-4:],
        }
        print(f"[verify] D 驳回/{mode:6s}/{label:8s}: 状态={out[label]['order_status']} "
              f"execute次数={out[label]['execute_times']} "
              f"生成运单={out[label]['generated_shipment']} "
              f"尾部={out[label]['trace_tail']}", flush=True)
    return out


def _child(mode: str) -> dict:
    os.environ["SCA_SUPERVISOR_MODE"] = mode
    if os.environ.get("SUP_VERIFY_SHIM") == "1":
        # 屏蔽 langgraph，强制走零依赖垫片：动态路由（Command.goto）必须在两条引擎上
        # 表现一致，否则 `verify_p0p1.py --shim` 这条既有证据链会无声失效。
        import importlib.abc

        class _Block(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "langgraph" or fullname.startswith("langgraph."):
                    raise ImportError("blocked for shim verification")
                return None

        sys.meta_path.insert(0, _Block())
    os.environ.update({"SCA_USE_PG": "false", "SCA_DINGTALK_WEBHOOK": "",
                       "SCA_MEMORY_ENABLED": "false",
                       "SCA_DISPATCH_REVIEW_ENABLED": "false",
                       "SCA_GPS_AUTO_ADVANCE": "0", "SCA_NOTIFY_DESKTOP": "false",
                       "SCA_NOTIFY_DRY_RUN": "true"})
    _block_external_notify()
    import sca.mcp.map_server as ms
    import sca.mcp.route_llm as rl

    ms._amap_key = lambda: ""       # 关闭高德实时选路 -> 内置路网几何（可复现）

    def _boom(*_a, **_k):
        raise RuntimeError("verify: 模型通道已在确定性模式下关闭")

    rl._chat = _boom
    rl._chat_with_tools = _boom

    from sca._compat import USING_REAL_LANGGRAPH
    from sca.settings import settings

    status = {"engine": "real" if USING_REAL_LANGGRAPH else "shim",
              "supervisor_mode": settings.supervisor_mode}
    snaps = asyncio.run(_run_scenarios(mode, status))
    defects = asyncio.run(_run_defect_injection(status)) if mode == "on" else {}
    gate_reject = asyncio.run(_run_gate_reject(mode))
    return {"mode": mode, "status": status, "snapshots": snaps,
            "defects": defects, "gate_reject": gate_reject}


def _run_child(mode: str) -> dict:
    env = dict(os.environ)
    env["SCA_SUPERVISOR_MODE"] = mode
    env.update({"SCA_USE_PG": "false", "SCA_MEMORY_ENABLED": "false",
                "SCA_NOTIFY_DESKTOP": "false", "SCA_NOTIFY_DRY_RUN": "true"})
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--mode", mode],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=900)
    for line in (proc.stdout or "").splitlines():
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    return {"mode": mode, "fatal": f"子进程 {mode} 未产出结果 (rc={proc.returncode})\n"
                                   f"stdout={proc.stdout[-2000:]}\n"
                                   f"stderr={proc.stderr[-2000:]}"}


def child_main(mode: str) -> int:
    payload = _child(mode)
    print(RESULT_MARK + json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=_MODES, default="")
    ap.add_argument("--shim", action="store_true",
                    help="屏蔽 langgraph，三模式全部走零依赖垫片（双引擎等价验证）")
    args = ap.parse_args()
    if args.shim:
        os.environ["SUP_VERIFY_SHIM"] = "1"
    if args.mode:
        return child_main(args.mode)

    fails: list = []
    print("=" * 78)
    print("A. 纯函数层：eligible / decide / legacy_route / validate_targets / 不变量")
    print("=" * 78)
    fa = part_a()
    for f in fa:
        print("   FAIL", f)
    print(f"   -> {'全部通过' if not fa else f'{len(fa)} 条失败'}")
    fails += fa

    print("\n" + "=" * 78)
    print("B. 三模式图级运行（off 基线 / shadow 只记录 / on 生效）")
    print("=" * 78)
    runs = {}
    for mode in _MODES:
        runs[mode] = _run_child(mode)
    fatal = [(m, r.get("fatal")) for m, r in runs.items() if r.get("fatal")]
    for m, msg in fatal:
        print(f"   FATAL [{m}] {msg}")
    if fatal:
        return 2

    for mode in _MODES:
        print(f"   {mode:6s} 引擎={runs[mode]['status']['engine']} "
              f"settings.supervisor_mode={runs[mode]['status']['supervisor_mode']!r}")

    # B1 shadow 必须与 off 逐字段一致
    for scen in SCENARIOS:
        a = dict(runs["off"]["snapshots"][scen])
        b = dict(runs["shadow"]["snapshots"][scen])
        a.pop("_sup", None)
        b.pop("_sup", None)
        if a != b:
            diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
            for k in diff:
                fails.append(f"B1 shadow[{scen}].{k} 与 off 不一致："
                             f"{json.dumps(a.get(k), ensure_ascii=False)[:160]} != "
                             f"{json.dumps(b.get(k), ensure_ascii=False)[:160]}")
        else:
            print(f"   [B1] shadow[{scen}] 与 off 业务快照一致 OK")

    # B2 shadow 必须真的在记录决策，且记录的是 shadow 适配器
    for scen in SCENARIOS:
        sup = runs["shadow"]["snapshots"][scen]["_sup"]
        if sup["rounds"] == 0:
            fails.append(f"B2 shadow[{scen}] 没有任何 supervisor 决策留痕")
        elif sup["modes"] != ["shadow"]:
            fails.append(f"B2 shadow[{scen}] 留痕 mode 异常：{sup['modes']}")
        else:
            hit = sum(1 for x in sup["match"] if x)
            print(f"   [B2] shadow[{scen}] 决策轮次={sup['rounds']} "
                  f"与原拓扑一致 {hit}/{sup['rounds']} 决策={sup['decisions']}")

    # B3 on 模式必须跑完、不停滞、决策序列合法
    for scen in SCENARIOS:
        sup = runs["on"]["snapshots"][scen]["_sup"]
        if sup["stalled"]:
            fails.append(f"B3 on[{scen}] 出现规划段停滞（落到 end）")
        if sup["rounds"] == 0:
            fails.append(f"B3 on[{scen}] 没有任何 supervisor 决策留痕")
        print(f"   [B3] on[{scen}] 决策轮次={sup['rounds']} 落点={sup['gotos']} "
              f"验收={sup['evaluated']} 停滞={sup['stalled']}")

    # B4 on 模式业务结果应与 off 一致（竖切片不改变业务结论，只改变编排）
    for scen in SCENARIOS:
        a = dict(runs["off"]["snapshots"][scen])
        b = dict(runs["on"]["snapshots"][scen])
        a.pop("_sup", None)
        b.pop("_sup", None)
        if a != b:
            diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
            print(f"   [B4] on[{scen}] 与 off 有 {len(diff)} 处业务差异：{diff}")
            for k in diff:
                fails.append(f"B4 on[{scen}].{k}："
                             f"{json.dumps(a.get(k), ensure_ascii=False)[:160]} != "
                             f"{json.dumps(b.get(k), ensure_ascii=False)[:160]}")
        else:
            print(f"   [B4] on[{scen}] 与 off 业务快照一致 OK")

    print("\n" + "=" * 78)
    print("C. 故障注入：校验闸口是真在拦人（且不死锁）")
    print("=" * 78)
    d = runs["on"].get("defects") or {}
    if not d:
        fails.append("C 未取得故障注入结果")
    else:
        for label, want_ship in (("放行", True), ("驳回", False)):
            r = d.get(label) or {}
            print(f"   [C] 人工{label}: 重派次数={r.get('attempts')} "
                  f"转人工={r.get('needs_human')} 生成运单={r.get('generated_shipment')} "
                  f"运单状态={r.get('order_status')}")
            if not r.get("needs_human"):
                fails.append(f"C[{label}] 缺陷未被升级为 needs_human（校验没拦住）")
            if bool(r.get("generated_shipment")) != want_ship:
                fails.append(f"C[{label}] 下发行为与预期不符（期望下发={want_ship}）")
            # 放行 -> 下发成功，随后 monitor 把运单推进到 in_transit；
            # 驳回 -> 闸口直接收口（**不再经过 execute**），运单标 approval_rejected。
            want_status = "in_transit" if want_ship else "approval_rejected"
            if r.get("order_status") != want_status:
                fails.append(f"C[{label}] 运单状态期望 {want_status} 实得 {r.get('order_status')}")
        # 重派一次：attempts 应为 2（首次 + 重试一次），证明"重试"确实发生且只发生一次
        if (d.get("放行") or {}).get("attempts", {}).get("plan_arbiter") != 2:
            fails.append(f"C 重派次数异常：{(d.get('放行') or {}).get('attempts')}")

    print("\n" + "=" * 78)
    print("D. 审批驳回：不发起下发 / 不重复下发，且三种模式口径一致")
    print("=" * 78)
    dj = {m: (runs[m].get("gate_reject") or {}) for m in _MODES}
    for mode in _MODES:
        r = dj.get(mode) or {}
        if not r:
            fails.append(f"D 未取得 {mode} 模式的驳回结果")
            continue
        # 规划段驳回：一次都不许下发
        p = r.get("plan") or {}
        if p.get("execute_times"):
            fails.append(f"D[{mode}/plan] 驳回后仍走到了 execute（驳回被当成放行）")
        if p.get("generated_shipment"):
            fails.append(f"D[{mode}/plan] 驳回后仍生成了 TMS 运单")
        if p.get("order_status") != "approval_rejected":
            fails.append(f"D[{mode}/plan] 状态期望 approval_rejected 实得 {p.get('order_status')}")
        if not (p.get("trace_tail") or [""])[-1] == "end":
            fails.append(f"D[{mode}/plan] 驳回后未收口到 end：{p.get('trace_tail')}")
        # 处置段驳回：已下发的那一次不能被抹掉，也不许再来一次
        d = r.get("disposal") or {}
        if d.get("execute_times") != 1:
            fails.append(f"D[{mode}/disposal] execute 次数期望 1 实得 {d.get('execute_times')}"
                         f"（>1 = 驳回后重复下发；0 = 该场景本就没下发，用例失效）")
        if not (d.get("trace_tail") or [""])[-1] == "end":
            fails.append(f"D[{mode}/disposal] 驳回后未收口到 end：{d.get('trace_tail')}")
    # 只比对**业务结论**：trace 里的节点名属于拓扑，`on` 模式多两个节点是设计使然。
    keys = ("order_status", "generated_shipment", "execute_times", "has_notify")
    sig = {m: json.dumps({k: {kk: (v or {}).get(kk) for kk in keys}
                          for k, v in (r or {}).items()},
                         sort_keys=True, ensure_ascii=False)
           for m, r in dj.items() if r}
    for m, r in dj.items():
        for k in ("plan", "disposal"):
            v = (r or {}).get(k) or {}
            print(f"   [D] {m:6s}/{k:8s} 状态={v.get('order_status')} "
                  f"execute次数={v.get('execute_times')} 生成运单={v.get('generated_shipment')}")
    if len(set(sig.values())) > 1:
        fails.append("D 三模式驳回结论不一致：" + json.dumps(
            {m: {k: {kk: (v or {}).get(kk) for kk in keys} for k, v in (r or {}).items()}
             for m, r in dj.items()}, ensure_ascii=False))
    else:
        print("   [D] off/shadow/on 结论一致：规划段驳回 0 次下发、"
              "处置段驳回不重复下发、两者都收口到 end")

    print("\n" + "=" * 78)
    if fails:
        print(f"结果：FAIL，共 {len(fails)} 条问题")
        for f in fails[:60]:
            print("   -", f)
        if len(fails) > 60:
            print(f"   ... 另有 {len(fails) - 60} 条")
        return 1
    print("结果：PASS —— 纯函数断言、shadow 与 off 等价、on 无停滞、校验闸口闭环、驳回不下发")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
