"""LangGraph 多 Agent 状态机（需求 §6）与节点实现。

三层循环：规划层（订单→运力匹配→路径规划→审批）、执行层（下发→监控→异常检测→
处置/重规划）、反思层（复盘→指标→知识库）。HITL 用 LangGraph interrupt + 检查点恢复
（需求 §8/§10）。

实现口径（全部节点为真实业务逻辑，无占位返回）：
  * 外部动作一律通过 MCP 工具调用（tms/gps/weather/notify/map/capacity），调用留证据；
  * 遥测异常检测是确定性规则引擎（温控越界/车门/停留/偏航/ETA 超时/天气），可单测；
  * replan_ms 为 perf_counter 实测值（预算口径单独给 replan_budget_ms）；
  * 复盘产出案例并落盘 data/cases.json（知识沉淀 FR-09）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sca._compat import StateGraph, END, Command, interrupt

from sca.intent import build_intent_model, IntentResult
from sca.simulation import OrderGenerator, FaultInjector
from sca.core.decision import route_action, estimate_replan_ms, sla_target
from sca.state import SupplyChainState
from sca.mcp.client import call as mcp_call
from sca.mcp.notify_server import valid_phone as _valid_phone, mask_phone as _mask_phone
from sca.settings import settings
# —— Supervisor 化（S3 竖切片）：决策/验收/路由复刻都在 sca.supervisor 包里 ——
# 该包只依赖标准库（不 import sca._compat / sca.agents），因此这里顶层导入无循环风险。
from sca.supervisor import (Decision, decide, decision_sig, eligible, evaluate,
                            gate_passed, handoff_target, legacy_route,
                            pending_workers, stall_reasons, validate_targets)


# 单一决策来源：与仿真评估引擎共用 sca.core.decision（保证方法链一致）
def _needs_approval(state: SupplyChainState) -> bool:
    """高危判定（P0/P1）。

    转调 `sca.supervisor.precondition.needs_approval` —— 这条规则现在有三个读者
    （本文件的条件边、`approval_gate` 节点、supervisor 的 ELIGIBLE 表），口径必须只有
    一处，否则"条件边放行、supervisor 拦下"这类分叉会静默出现。
    """
    from sca.supervisor.precondition import needs_approval as _judge
    return _judge(state)


def _gate_ok(state: SupplyChainState) -> bool:
    """人工闸口是否放行（auto / approved）。驳回一律视为未放行。"""
    return gate_passed(state.get("approval"))


def _after_gate(state: SupplyChainState) -> str:
    """`approval_gate` 的出边：**放行才下发，驳回一律收口**。

    2026-10-17 修正一处松口：改造前这里是静态边 `approval_gate -> execute`，
    意味着"调度员驳回"和"调度员批准"走的是同一条路——驳回等于放行，真会落 TMS
    运单。而 `on` 模式因 supervisor 要求 `approved` 才收口到 `end`，两个模式对
    同一件事给出相反结论。现在统一为更严的一条口径：`execute` 只接受显式放行。
    """
    return "execute" if _gate_ok(state) else "end"


def _gate_result(s: SupplyChainState, apr: Any, trace: List[str]) -> Dict[str, Any]:
    """把闸口结论落地成节点返回值。

    驳回时把**尚未下发**的运单标成 `approval_rejected`（否则订单状态还停在
    "已规划"，看起来像正常走完了）。已下发过的（处置段二次审批被驳回）不改状态——
    运单已在途，改状态会让记录与事实矛盾。
    """
    out: Dict[str, Any] = {"approval": apr, "trace": trace}
    if not gate_passed(apr if isinstance(apr, dict) else None) and not s.get("execution"):
        out["order"] = {**(s.get("order") or {}), "status": "approval_rejected"}
    return out

logger = logging.getLogger("sca.agents")

_CASES_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "cases.json"

# 意图/工具注册表（供 TongyiIntentDetectV3 的 INTENT_MODE 使用，FR-12）
TOOL_REGISTRY = [
    {"name": "order.query", "description": "查询运单状态"},
    {"name": "order.create", "description": "创建运单并下发"},
    {"name": "capacity.match", "description": "按货物/路线匹配运力 Top-N"},
    {"name": "plan.replan", "description": "动态重规划路径"},
    {"name": "exception.detect", "description": "在途异常检测"},
    {"name": "weather.query", "description": "查询路线天气/预警"},
]


def _route_ends(order: Dict[str, Any]) -> tuple:
    """取起讫地：优先订单的 origin/dest（需求解析产出），兼容旧 route_hint。

    取不到时返回 (None, None) —— 调度需求里没有的就是没有，不套用任何默认线路。
    """
    o = (order or {}).get("origin")
    d = (order or {}).get("dest")
    if o and d:
        return str(o), str(d)
    hint = str((order or {}).get("route_hint") or "")
    for sep in ("→", "->", "至", "到"):
        if sep in hint:
            a, b = hint.split(sep, 1)
            a, b = a.strip(), b.strip()
            return (a or None), (b or None)
    return None, None


# ---------------------------------------------------------------------------
# 遥测异常规则引擎（FR-05/FR-06，确定性、可单测）
# ---------------------------------------------------------------------------
# 温控阈值（按货类，℃）：越界即异常
_TEMP_RANGES: Dict[str, tuple] = {
    "生鲜": (0, 4), "冷链": (0, 4), "医药": (2, 8), "疫苗": (2, 8),
    "危险品": (5, 30), "普货": None,
}
_LEVEL_SEVERITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _fault(code: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """按异常码从故障谱构建异常事件（规则引擎命中）。"""
    from sca.simulation.fault_spectrum import by_code
    f = by_code(code)
    base = {"code": f.code, "name": f.name, "level": f.level, "desc": f.desc,
            "subsystems": list(f.subsystems), "auto": f.auto, "ts": int(time.time()),
            "source": "rule", "evidence": evidence} if f else {
        "code": code, "name": code, "level": "P2", "desc": "", "subsystems": [],
        "auto": True, "ts": int(time.time()), "source": "rule", "evidence": evidence}
    return base


def _temp_range_for(order: Dict[str, Any]) -> Optional[tuple]:
    cargo_type = str(((order or {}).get("cargo") or {}).get("type") or "普货")
    rng = _TEMP_RANGES.get(cargo_type)
    if rng is None and (order or {}).get("temp_ctrl"):
        rng = _TEMP_RANGES["冷链"]  # 温控货物未注明品类，按冷链 0~4℃
    return rng


def _deadline_remaining_h(order: Dict[str, Any]) -> Optional[float]:
    """解析交货截止 -> 距今剩余小时数。

    **优先用结构化字段**（intake 解析出的 `start_clock` + `duration_days`）：
    调度需求"今日20点整起4天"是**起运时刻 + 履约天数**，不是"20:00 截止"。
    旧实现只抓文本里第一个 HH:MM 当截止，于是"20点整起4天"被误判成"距 20:00
    只剩 3.7h"，而 ETA 9.3h > 3.7h -> 凭空判出 P1 时效违约（2026-10-15 实测）。
    无结构化字段时退回文本解析，并加"起运语境"守卫：含"起/发车/出发/装车"
    却无"前/送达/抵达/交货"时，该时刻是起运时刻而非截止，不据此判违约。
    """
    import re
    o = order or {}
    rb = str(o.get("required_by") or "")
    now = datetime.now()

    def _day_offset(text: str) -> int:
        if "后天" in text:
            return 2
        if any(k in text for k in ("次日", "明天", "明日")):
            return 1
        return 0

    def _parse_hhmm(text: str) -> Optional[tuple]:
        m = re.search(r"(\d{1,2})[:：点时](\d{2})?", text)
        if not m:
            return None
        return min(int(m.group(1)), 23), int(m.group(2) or 0)

    # 1) 结构化字段优先：截止 = 起运时刻 + 履约天数
    start_clock = str(o.get("start_clock") or "").strip()
    duration = o.get("duration_days")
    if start_clock and isinstance(duration, (int, float)) and float(duration) > 0:
        hhmm = _parse_hhmm(start_clock)
        if hhmm:
            hh, mm = hhmm
            base = (now + timedelta(days=_day_offset(rb or start_clock))).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            deadline = base + timedelta(days=float(duration))
            return round((deadline - now).total_seconds() / 3600, 2)

    if not rb:
        return None
    # 2) 起运语境守卫：非"截止"口径时，不据此判违约
    if re.search(r"起|发车|出发|装车|起运", rb) and not re.search(
            r"前|之前|以前|送达|抵达|交货|到达|到货|必须到", rb):
        return None
    hhmm = _parse_hhmm(rb)
    if hhmm is None:
        md = re.search(r"(\d+)\s*(?:个)?\s*(?:天|日|自然日)内", rb)
        return round(float(md.group(1)) * 24.0, 2) if md else None
    hh, mm = hhmm
    off = _day_offset(rb)
    if off == 0 and not any(k in rb for k in ("今天", "当日", "当天", "今晚",
                                              "下午", "上午", "中午")):
        off = 0 if (now.hour * 60 + now.minute) <= (hh * 60 + mm) else 1
    deadline = (now + timedelta(days=off)).replace(hour=hh, minute=mm,
                                                   second=0, microsecond=0)
    return round((deadline - now).total_seconds() / 3600, 2)


def _detect_rules(order: Dict[str, Any], telemetry: Dict[str, Any],
                  weather: Dict[str, Any], eta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """规则引擎：遥测/天气/时效 -> 异常事件清单（按等级严重度排序）。"""
    detected: List[Dict[str, Any]] = []
    tel = telemetry or {}
    cargo_type = str((order or {}).get("cargo", {}).get("type") or "普货")

    # R1 温控越界（按货类阈值）
    rng = _temp_range_for(order)
    temp = tel.get("temp_c")
    if rng and isinstance(temp, (int, float)):
        lo, hi = rng
        if temp > hi:
            code = "E-DG-OVER" if cargo_type == "危险品" else "E-COLD-OVER"
            detected.append(_fault(code, {"rule": "temp_out_of_range",
                                          "temp_c": temp, "allowed": f"{lo}~{hi}℃"}))
        elif temp < lo:
            detected.append(_fault("E-COLD-DRIFT", {"rule": "temp_out_of_range",
                                                    "temp_c": temp,
                                                    "allowed": f"{lo}~{hi}℃"}))
    # R2 温控货物门未关
    if rng and tel.get("door_closed") is False:
        detected.append(_fault("E-COLD-DOOR", {"rule": "door_open",
                                               "cargo_type": cargo_type}))
    # R3 停留超时（低速持续）
    dwell = tel.get("dwell_min") or 0
    if isinstance(dwell, (int, float)) and dwell >= 120:
        detected.append(_fault("E-TMS-STOP", {"rule": "dwell_timeout",
                                              "dwell_min": dwell, "threshold_min": 120}))
    elif isinstance(dwell, (int, float)) and dwell >= 45:
        detected.append(_fault("E-JAM", {"rule": "dwell_slow",
                                         "dwell_min": dwell, "threshold_min": 45}))
    # R4 ETA 超时（预估到达时间晚于客户时效要求）
    est_h = (eta or {}).get("est_h")
    remaining = _deadline_remaining_h(order)
    if est_h and remaining is not None and float(est_h) > remaining:
        detected.append(_fault("E-SLA-DELAY", {"rule": "eta_overdue",
                                               "eta_h": est_h,
                                               "deadline_remaining_h": remaining}))
    # R5 路线天气（预报/预警文本命中）
    wx_text = " ".join(str(x) for x in [
        (weather or {}).get("condition"), (weather or {}).get("warning")])
    for kw, code in (("暴雨", "E-WX-RAIN"), ("暴雪", "E-WX-SNOW"),
                     ("台风", "E-WX-TYPHOON"), ("大雾", "E-WX-FOG"),
                     ("高温", "E-WX-HOT")):
        if kw in wx_text:
            detected.append(_fault(code, {"rule": "weather", "text": wx_text[:60]}))
            break

    detected.sort(key=lambda e: _LEVEL_SEVERITY.get(e.get("level"), 9))
    return detected


# ---------------------------------------------------------------------------
# 案例落盘（FR-09 知识沉淀）
# ---------------------------------------------------------------------------
def _append_case(case: Dict[str, Any]) -> str:
    """追加案例到 data/cases.json（复盘知识沉淀，供后续检索/训练）。"""
    try:
        _CASES_FILE.parent.mkdir(parents=True, exist_ok=True)
        cases = []
        if _CASES_FILE.is_file():
            try:
                cases = json.loads(_CASES_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa
                cases = []
        cases.append(case)
        if len(cases) > 500:  # 环形保留最近 500 条
            cases = cases[-500:]
        _CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        return case["case_id"]
    except Exception as exc:  # noqa
        logger.warning("[agents] 案例落盘失败：%s", exc)
        return ""


class SupplyChainGraph:
    def __init__(self, intent_model=None):
        self.intent = intent_model or build_intent_model()
        self.gen = OrderGenerator()
        self.fault = FaultInjector()

    # ---- 规划层 ----
    async def receive_order(self, s: SupplyChainState) -> Dict[str, Any]:
        order = s.get("order") or self.gen.next()
        trace = ["receive_order"]  # reducer 通道：只追加本节点记录
        return {"order_id": order["order_id"], "order": order, "plan_version": 1,
                "trace": trace}

    async def load_context(self, s: SupplyChainState) -> Dict[str, Any]:
        """装载执行上下文：真实读取该订单当前 IoT 遥测基线（gps-mcp）。"""
        trace = ["load_context"]  # reducer 通道：只追加本节点记录
        cargo = s["order"].get("cargo", {})
        r = await mcp_call("gps-mcp", "get_telemetry", order_id=s["order_id"])
        telemetry = r.data if (r.ok and isinstance(r.data, dict)) else {"unavailable": True}
        return {"cargo": cargo, "telemetry": {"baseline": telemetry},
                "mcp_calls": [r.to_dict()], "trace": trace}

    async def intent_route(self, s: SupplyChainState) -> Dict[str, Any]:
        """意图路由（FR-12）：用调度员原话做意图识别 + 工具路由预判。"""
        trace = ["intent_route"]  # reducer 通道：只追加本节点记录
        o, d = _route_ends(s["order"])
        hint = s["order"].get("route_hint") or (f"{o}→{d}" if o and d else "")
        # 调度员原话优先；无原话（如 API 显式传 order）才用摘要文本
        text = str(s.get("source_text") or "").strip() or \
            f"{hint} {s['order'].get('cargo', {}).get('type')} 订单".strip()
        res: IntentResult = await self.intent.classify(text, TOOL_REGISTRY, {})
        return {"intent": {"intent": res.intent, "confidence": res.confidence,
                           "source": res.source, "input_text": text[:80]},
                "trace": trace}

    # ---- 规划层（P1 并行扇出：plan_prepare -> [agent_route ∥ agent_precheck] -> plan_arbiter）----
    async def plan_prepare(self, s: SupplyChainState) -> Dict[str, Any]:
        """规划段准备（扇出前，独占写 plan_context）：解析起讫/货类/温控/危品/重量口径。

        为什么单独一步：下面两个 Agent 需要同一份上下文。在此解析一次，避免两个并行
        分支各自重复解析而口径漂移；路线走确定性还是模型增强版也在此判定。
        """
        trace = ["plan_prepare"]  # reducer 通道：只追加本节点记录
        order = s.get("order") or {}
        cargo = order.get("cargo") or {}
        o, d = _route_ends(order)
        cargo_type = cargo.get("type") or "普货"
        temp_ctrl = bool(order.get("temp_ctrl") or cargo_type == "冷链")
        hazmat = cargo_type == "危险品"
        # 重量未填报时不编造：留空并在匹配时按最小量级假设（结果中标注待补录）
        weight_t = cargo.get("weight_t")
        weight_for_match = float(weight_t) if weight_t not in (None, "") else 1.0

        # 调度员原话含路线偏好词时走模型增强版 plan_route_smart（S2/S4 模型介入），
        # 否则走确定性 plan_route（零额外成本）；两者返回结构兼容，模型全程 fail-open。
        nl_req = str(s.get("source_text") or "").strip()
        _route_kw = ("高速", "山区", "隧道", "国道", "夜间", "白天", "优先", "避开",
                     "绕行", "尽量", "成本", "便宜", "稳妥", "风险")
        # 有异常上报时，原话里的"高速/绕行/避开"属**异常语境**而非路线偏好
        # （如"高速封路""施工绕行"）。若据此把初始方案也交给模型，会带来两个问题：
        # ① 同一需求两次跑基线里程不一致（模型非确定性，实测 884.9 / 1046.7 两种结果）；
        # ② 异常避让在"初始计划"就被消耗掉，replan 变成空转（"维持原走廊"）。
        # 故有异常时初始计划走确定性 plan_route，避让统一交给 replan（2026-10-15）。
        _exc_present = bool((s.get("exception") or {}).get("code"))
        use_smart = (not _exc_present) and bool(nl_req) \
            and any(k in nl_req for k in _route_kw)
        # 管理页面的「模型降级工厂」开关在这里生效：关掉 route 模型后，
        # 即便原话里有路线偏好词也走确定性算法，而不是偷偷继续调模型。
        from sca import runtime_flags

        if use_smart and not runtime_flags.get("route_model", True):
            use_smart = False

        ctx = {"origin": o, "dest": d, "cargo_type": cargo_type, "temp_ctrl": temp_ctrl,
               "hazmat": hazmat, "weight_t": weight_t, "weight_for_match": weight_for_match,
               "natural_lang_req": nl_req, "use_smart": use_smart,
               "missing_endpoints": not (o and d)}
        return {"plan_context": ctx, "trace": trace}

    async def agent_route(self, s: SupplyChainState) -> Dict[str, Any]:
        """路线 Agent（规划段并行分支①，独占写 route 分区）：map-mcp 路径规划。

        与 agent_precheck 互不依赖，因此被放在同一超步并行执行；本节点只写自己的
        route 分区，不碰 current_plan 等决策字段（写权归 plan_arbiter）。
        """
        t0 = time.time()
        trace = ["agent_route"]  # reducer 通道：只追加本节点记录
        ctx = s.get("plan_context") or {}
        o, d = ctx.get("origin"), ctx.get("dest")

        def _log(**kw) -> List[Dict[str, Any]]:
            return [{"agent": "agent_route", "t_start": t0, "t_end": time.time(),
                     "ms": round((time.time() - t0) * 1000, 1), **kw}]

        if not (o and d):   # 需求未提供起讫：不调用 map-mcp，也不编造路径
            return {"route": {"waypoints": [], "distance_km": 0, "origin": o, "dest": d},
                    "mcp_calls": [], "trace": trace,
                    "agent_log": _log(calls=0, skipped="missing_endpoints")}

        cargo_type = ctx.get("cargo_type") or "普货"
        temp_ctrl, hazmat = bool(ctx.get("temp_ctrl")), bool(ctx.get("hazmat"))
        if ctx.get("use_smart"):
            r_route = await mcp_call("map-mcp", "plan_route_smart", origin=o, dest=d,
                                     cargo_type=cargo_type, temp_ctrl=temp_ctrl, hazmat=hazmat,
                                     natural_lang_req=ctx.get("natural_lang_req") or "")
        else:
            r_route = await mcp_call("map-mcp", "plan_route", origin=o, dest=d,
                                     cargo_type=cargo_type, temp_ctrl=temp_ctrl, hazmat=hazmat)
        calls: List[Dict[str, Any]] = [r_route.to_dict()]
        route = r_route.data if (r_route.ok and isinstance(r_route.data, dict)
                                 and r_route.data.get("ok", True)) else None
        degraded_reason = ""
        if not route:  # map-mcp 不可用 / 起讫无法解析 / 同城 -> 降级：不编造路径，仅标注原因
            degraded_reason = (r_route.data.get("error") if isinstance(r_route.data, dict)
                               else None) or r_route.error or "map-mcp 未返回可用路径"
            route = {"waypoints": [o, d], "distance_km": 0, "eta_h": None,
                     "cost": {"est": 0}, "provider": "unavailable"}

        out_route = {"waypoints": route.get("waypoints", []),
                     "distance_km": float(route.get("distance_km") or 0),
                     "origin": o, "dest": d, "provider": route.get("provider"),
                     # 以下两项是本分区产出、供 plan_arbiter 求解链式依赖用（eta/成本初值）
                     "eta_h": route.get("eta_h"),
                     "cost_est": float((route.get("cost") or {}).get("est") or 0)}
        # 仅在真降级时补这两个键，正常路径保持与原实现完全一致的字段集
        if degraded_reason:
            out_route["degraded"] = True
            out_route["degraded_reason"] = degraded_reason
        if route.get("notes"):   # 省份名按省会映射等提示
            out_route["notes"] = route["notes"]
        return {"route": out_route, "mcp_calls": calls, "trace": trace,
                "agent_log": _log(calls=len(calls), ok=True, degraded=bool(degraded_reason),
                                  distance_km=out_route["distance_km"],
                                  provider=out_route.get("provider"))}

    async def agent_precheck(self, s: SupplyChainState) -> Dict[str, Any]:
        """前置取证 Agent（规划段并行分支②，独占写 precheck 分区）。

        与路线规划互不依赖，故被放在同一超步并行执行——其耗时会被路线规划吸收，
        不占用关键路径。产出两项独立证据：
          ① 订单核验（tms-mcp.query_order）：该单在 TMS 是否已存在、当前状态。
             价值：提前暴露重复下发，不必等到 execute 才以"运单已存在"失败。
          ② 货类合规检索（knowledge-mcp.search_knowledge）：按货类/温控预检操作要求。
             与 retrieve_knowledge 不重复——后者是按**异常码**检索处置 SOP。

        只写 precheck 分区，**不写 weather / telemetry 等下游判定字段**，故对既有
        异常判定链零影响。取证失败一律如实标注（degraded / notes），不编造结论。
        """
        t0 = time.time()
        trace = ["agent_precheck"]  # reducer 通道：只追加本节点记录
        ctx = s.get("plan_context") or {}
        order = s.get("order") or {}
        oid = str(order.get("order_id") or "")
        calls: List[Dict[str, Any]] = []
        pre: Dict[str, Any] = {"query": {}, "degraded": False, "notes": []}

        # ① 订单核验（tms-mcp）
        if oid:
            r = await mcp_call("tms-mcp", "query_order", order_id=oid)
            calls.append(r.to_dict())
            data = r.data if isinstance(r.data, dict) else {}
            if r.ok and data.get("ok"):
                pre["order_check"] = {"exists": True, "status": data.get("status"),
                                      "carrier_id": data.get("carrier_id")}
                pre["notes"].append(f"该单已在 TMS（状态：{data.get('status')}）")
            elif r.ok and data.get("error") == "not_found":
                pre["order_check"] = {"exists": False}
            else:
                pre["order_check"] = {"exists": None,
                                      "error": r.error or str(data.get("error") or "")}
                pre["degraded"] = True
                pre["notes"].append("订单核验不可用")
        else:
            pre["order_check"] = {"exists": None, "error": "缺少订单号"}
            pre["degraded"] = True

        # ② 货类合规检索（knowledge-mcp）
        ct = ctx.get("cargo_type") or "普货"
        q = f"{ct} 运输 操作要求" + (" 温控" if ctx.get("temp_ctrl") else "")
        pre["query"]["compliance"] = q
        r2 = await mcp_call("knowledge-mcp", "search_knowledge", query=q, top_k=2)
        calls.append(r2.to_dict())
        hits = r2.data if (r2.ok and isinstance(r2.data, list)) else None
        if hits is not None:
            pre["compliance"] = [{"doc_id": h.get("doc_id"),
                                  "score": round(float(h.get("score") or 0), 3),
                                  "content": str(h.get("content") or "")[:160]}
                                 for h in hits[:2] if isinstance(h, dict)]
            if not pre["compliance"]:
                pre["notes"].append(f"知识库未命中「{ct}」相关条目")
        else:
            pre["compliance"] = None
            pre["degraded"] = True
            pre["notes"].append("货类合规检索不可用")

        return {"precheck": pre, "mcp_calls": calls, "trace": trace,
                "agent_log": [{"agent": "agent_precheck", "t_start": t0,
                               "t_end": time.time(),
                               "ms": round((time.time() - t0) * 1000, 1),
                               "calls": len(calls), "ok": True,
                               "order_exists": (pre.get("order_check") or {}).get("exists"),
                               "compliance_hits": len(pre.get("compliance") or []),
                               "degraded": pre["degraded"]}]}

    async def plan_arbiter(self, s: SupplyChainState) -> Dict[str, Any]:
        """规划仲裁（扇入汇聚，独占写 current_plan / capacity / cost / eta）。

        三件必须由它做、不能下放给并行 Agent 的事：
          ① 写权限收口：LangGraph 中并行分支写同一 state 字段会被拒绝，故 agent_route /
             agent_precheck 各写自己分区（route / precheck），决策字段只能有唯一写者。
          ② 跨域依赖求解：运力匹配需要路线的 distance_km，成本精算需要选中承运商的
             cost_per_km，路线下发需要 waypoints —— 三者是链式依赖，必须在一处按序求解。
          ③ 统一裁决与留痕：把各路结论、选定依据、降级原因汇总进 plan_arbitration，
             供前端/审核卡说明"为什么是这个方案"；裁决依据是可核对的证据，不是模型输出。
        """
        t0 = time.time()
        trace = ["plan_arbiter"]  # reducer 通道：只追加本节点记录
        ctx = s.get("plan_context") or {}
        order = s.get("order") or {}
        route = s.get("route") or {}
        pre = s.get("precheck") or {}
        o, d = ctx.get("origin"), ctx.get("dest")
        cargo_type = ctx.get("cargo_type") or "普货"
        temp_ctrl = bool(ctx.get("temp_ctrl"))
        hazmat = bool(ctx.get("hazmat"))
        weight_t = ctx.get("weight_t")
        weight_for_match = float(ctx.get("weight_for_match") or 1.0)
        oid = str(s.get("order_id") or order.get("order_id") or "")

        pre_summary = {"order_check": pre.get("order_check"),
                       "compliance_hits": len(pre.get("compliance") or []),
                       "degraded": pre.get("degraded"),
                       "notes": pre.get("notes") or []}

        def _log(**kw) -> List[Dict[str, Any]]:
            return [{"agent": "plan_arbiter", "t_start": t0, "t_end": time.time(),
                     "ms": round((time.time() - t0) * 1000, 1), **kw}]

        # 起讫缺失：与原实现一致，跳过一切外部调用（需求没说的不猜）
        if not (o and d):
            reason = "需求未提供起运地/目的地，无法规划路径（待补录）"
            return {"capacity": [], "current_plan": {
                        "route": None, "waypoints": [], "distance_km": 0,
                        "carriers": [], "carrier": None, "version": 1,
                        "degraded": True, "weight_assumed": weight_t is None,
                        "degraded_reason": reason},
                    "cost": {"est": 0, "currency": "CNY"}, "eta": {"est_h": None},
                    "plan_arbitration": {"agents": ["agent_route", "agent_precheck"],
                                         "missing_endpoints": True, "degraded": True,
                                         "reason": reason, "precheck": pre_summary},
                    "trace": trace, "agent_log": _log(calls=0, degraded=True,
                                                      skipped="missing_endpoints")}

        calls: List[Dict[str, Any]] = []
        distance_km = float(route.get("distance_km") or 0)

        # 1) 运力匹配（capacity-mcp 多因子加权）
        # order_id：调度员在异常描述区录入的订单号。命中"正承运该单"的承运商时，
        # capacity-mcp 会把它置顶——异常处置沿用原车原司机，不因一次重算就换承运商。
        r_cap = await mcp_call("capacity-mcp", "match_capacity", cargo_type=cargo_type,
                               route=f"{o}→{d}", top_n=3, weight_t=weight_for_match,
                               temp_ctrl=temp_ctrl, hazmat=hazmat, distance_km=distance_km,
                               order_id=str(order.get("order_id") or ""))
        calls.append(r_cap.to_dict())
        carriers = r_cap.data if (r_cap.ok and isinstance(r_cap.data, list) and r_cap.data) else []
        carrier = carriers[0] if carriers else None

        # 2) 成本精算（选定承运商单价）
        cost_est = float(route.get("cost_est") or 0)
        if carrier:
            r_cost = await mcp_call("map-mcp", "estimate_cost", distance_km=distance_km,
                                    cost_per_km=float(carrier.get("cost_per_km", 2.6)),
                                    temp_ctrl=temp_ctrl, hazmat=hazmat)
            calls.append(r_cost.to_dict())
            if r_cost.ok and isinstance(r_cost.data, dict):
                cost_est = float(r_cost.data.get("est") or cost_est)

        # 3) 路线下发（gps-mcp：对应生产 TMS→车载终端的路线同步，供偏航检测用）
        #    路径降级（起讫无法解析 / 同城 / MCP 不可用）时不下发，避免落 0 公里假数据
        if route.get("waypoints") and not route.get("degraded"):
            r_set = await mcp_call("gps-mcp", "set_route", order_id=oid,
                                   origin=o, dest=d, waypoints=route.get("waypoints"),
                                   distance_km=distance_km)
            calls.append(r_set.to_dict())

        plan = {"route": f"{o}→{d}", "waypoints": route.get("waypoints", []),
                "distance_km": distance_km, "carriers": carriers, "carrier": carrier,
                "version": 1, "provider": route.get("provider"),
                "weight_assumed": weight_t is None,
                "degraded": bool(route.get("degraded"))}
        # 订单号绑定结果：选中承运商是否就是该运单的在途承运商（供前端说明"为什么是它"）
        if carrier and carrier.get("bound_order_id"):
            plan["bound_order_id"] = str(carrier["bound_order_id"])
        if route.get("degraded_reason"):
            plan["degraded_reason"] = route["degraded_reason"]
        if route.get("notes"):   # 省份名按省会映射等提示
            plan["notes"] = route["notes"]

        arbitration = {
            "agents": ["agent_route", "agent_precheck"],
            "route": {"distance_km": distance_km, "provider": route.get("provider"),
                      "waypoints_n": len(route.get("waypoints") or []),
                      "degraded": bool(route.get("degraded"))},
            "capacity": {"selected": (carrier or {}).get("id"),
                         "top_n": len(carriers),
                         "reasons": (carrier or {}).get("match_reasons")},
            "cost_est": cost_est,
            "precheck": pre_summary,
            "degraded": bool(route.get("degraded")),
        }
        return {"capacity": carriers, "current_plan": plan,
                "cost": {"est": cost_est, "currency": "CNY"},
                "eta": {"est_h": route.get("eta_h")},
                "plan_arbitration": arbitration, "mcp_calls": calls, "trace": trace,
                "agent_log": _log(calls=len(calls), degraded=bool(route.get("degraded")),
                                  carrier=(carrier or {}).get("id"))}

    # ---- Supervisor 化（S3 竖切片）：规划段的"调度台"与"验收台" ----
    # 混合路由：出去动态（本节点返回 Command(goto=...)），回来静态（worker -> evaluator
    # -> supervisor）。为什么必须混合：LangGraph 的自动扇入只对**静态边**生效，若整个
    # 回路都靠 Command 动态跳转，汇聚点就等不到"两支都到达"，并行分支会各自往下跑。
    async def supervisor(self, s: SupplyChainState) -> Dict[str, Any]:
        """规划段调度台：决定下一个该谁做。

        本节点**不做决策**——决策全部来自纯函数 `sca.supervisor.decide()`（同源可
        复现），这里只做三件适配工作：
          ① 按 `SCA_SUPERVISOR_MODE` 选择路由来源：
             * `on`     -> 用决策结果 `Command(goto=...)`，真正改变路由；
             * `shadow` -> 用 `legacy_route()` 复刻**改造前那张静态图**的下一跳，
                           决策照算照记（含"是否与决策一致"），路由不受其影响；
          ② 硬校验目标合法性：官方引擎对未知 goto 目标**静默忽略**（图提前终止却不报错，
             把"没跑完"伪装成"跑完了"），所以必须在返回 Command 之前自己挡住；
          ③ 落痕：决策、依据、被否动作、ELIGIBLE 全集进 `supervisor_trace`。
        """
        mode = (getattr(settings, "supervisor_mode", "off") or "off").strip().lower()
        hist = list(s.get("supervisor_trace") or [])
        sig = decision_sig(s)

        d: Decision = decide(
            s, use_model=False, model=None,
            max_rounds=int(getattr(settings, "supervisor_max_rounds", 12) or 12),
            rounds_used=len(hist))
        # ⚠️ `use_model=False` 是**刻意写死**的，不是忘了接：
        # 目前只配了一个默认模型，让它在一个 1~2 项的 ELIGIBLE 集合里做"优先级选择"
        # 既无收益（集合太小、规则已确定）又有代价（多一次模型往返 + 决策不可复现，
        # 影子模式的"一致率"会变成噪声）。等接入多模型/多策略、ELIGIBLE 里真出现
        # 需要权衡的并列动作，再把这里换成 `settings.supervisor_model`。
        # 影子模式的对照基线：改造前那张图在该状态下的下一跳
        legacy = legacy_route(s)

        notes: List[str] = []
        goto = list(d.next_actions)
        if mode == "shadow":
            goto = legacy          # 路由照旧；决策只用于记录与比对
        else:
            bad = validate_targets(goto)
            if bad:
                notes.append(f"决策含非法目标 {bad}，已回退原拓扑目标 {legacy}")
                logger.error("[supervisor] %s", notes[-1])
                goto = legacy

        if not goto:               # 无可执行动作：规划段已结束，或出现停滞
            tgt = handoff_target(s)
            notes.append(f"ELIGIBLE 为空 -> {tgt}")
            if tgt == "end":
                logger.error("[supervisor] 规划段停滞：无可执行动作且未下发。逐条原因=%s",
                             stall_reasons(s))
            goto = [tgt]

        entry = {"mode": mode, "round": len(hist) + 1, "sig": sig,
                 "decision": d.to_dict(), "goto": list(goto),
                 "legacy_goto": legacy,
                 "match": sorted(d.next_actions) == sorted(legacy),
                 "notes": notes}
        out: Dict[str, Any] = {"supervisor_decision": d.to_dict(),
                               "loop_count": len(hist) + 1,
                               "next_action": list(goto),
                               "trace": ["supervisor"]}
        # 累加通道去重：fan-in 汇聚点在超步 replay 时可能重跑本节点，输入完全没变
        # （签名 + 落点都相同）就不重复留痕。
        if not (hist and hist[-1].get("sig") == sig and hist[-1].get("goto") == list(goto)):
            out["supervisor_trace"] = [entry]
        if mode == "shadow":
            logger.info("[supervisor/shadow] round=%s 决策=%s 原拓扑=%s 一致=%s 落点=%s",
                        entry["round"], d.next_actions, legacy, entry["match"], goto)
        # 必须返回 Command（本节点**没有声明任何静态出边**，路由权完全在这里）。
        # `Command.update` 承担状态写入，等价于普通节点返回 dict。
        return Command(goto=list(goto), update=out)

    async def plan_evaluator(self, s: SupplyChainState) -> Dict[str, Any]:
        """规划段验收台（扇入汇聚点）：prepare / route / precheck / arbiter 都指向它。

        验收结论落在 `evaluation`，并成为后续动作的前提条件——`plan_arbiter` 未
        accept 就不下发（`precondition._act_execute` 硬性要求）。所以"做完对不对"
        是真的在拦人，而不是走个过场。

        **无外部副作用**（不写文件、不发通知），因此超步 replay 重跑是安全的。
        """
        trace = ["plan_evaluator"]  # reducer 通道：只追加本节点记录
        ev = dict(s.get("evaluation") or {})
        accepted = dict(ev.get("accepted") or {})
        flagged = dict(ev.get("flagged") or {})
        needs_human = dict(ev.get("needs_human") or {})
        attempts = dict(ev.get("attempts") or {})

        reports: List[Dict[str, Any]] = []
        for worker in pending_workers(s):
            r = evaluate(worker, s)
            attempts[worker] = int(r.get("attempts") or 0)
            reports.append(r)
            verdict = r["verdict"]
            if verdict == "accept":
                accepted[worker] = True
                flagged.pop(worker, None)
                needs_human.pop(worker, None)
            elif verdict == "retry":
                flagged[worker] = list(r["defects"])
                needs_human.pop(worker, None)
                logger.warning("[evaluator] %s 产出有缺陷，重派一次：%s",
                               worker, r["defects"])
            else:
                flagged[worker] = list(r["defects"])
                needs_human[worker] = list(r["defects"])
                logger.error("[evaluator] %s 产出缺陷且重试机会用尽，转人工：%s",
                             worker, r["defects"])

        out_eval = {"accepted": accepted, "flagged": flagged, "needs_human": needs_human,
                    "attempts": attempts,
                    "verdicts": {r["worker"]: r["verdict"] for r in reports},
                    "defects": {r["worker"]: r["defects"] for r in reports if r["defects"]},
                    "last": reports}
        res: Dict[str, Any] = {"evaluation": out_eval, "trace": trace}
        if reports:
            res["agent_log"] = [{"agent": "plan_evaluator",
                                 "workers": [r["worker"] for r in reports],
                                 "verdicts": out_eval["verdicts"],
                                 "defects": out_eval["defects"]}]
        return res

    async def approval_gate(self, s: SupplyChainState) -> Dict[str, Any]:
        """人工闸口：高危审批（P0/P1）+ 规划产出校验未通过的升级确认，共用本节点。

        为什么共用一个节点：一次挂起只能有一个 `interrupt`（并行分支里挂起会丢载荷），
        而这两件事的语义都是"停下来问人"。合流在这里既省一个闸口，也天然满足
        "同批不得有两个 interrupt 节点"的约束。
        """
        trace = ["approval_gate"]  # reducer 通道：只追加本节点记录
        from sca._compat import interrupt
        nh = (s.get("evaluation") or {}).get("needs_human") or {}
        if nh and not (s.get("approval") or {}).get("escalation_resolved"):
            decision = interrupt({
                "reason": "规划产出未通过校验，需人工确认是否继续下发",
                "kind": "evaluation_escalation",
                "defects": nh,
            })
            apr = dict(decision) if isinstance(decision, dict) else {
                "approved": False, "reason": "调度员驳回"}
            apr["escalation_resolved"] = True   # 标记"问过了"，避免驳回后被反复弹框
            return _gate_result(s, apr, trace)
        if _needs_approval(s):
            decision = interrupt({"reason": "高危场景审批", "level": s["exception"]["level"]})
            apr = dict(decision) if isinstance(decision, dict) else decision
            if isinstance(apr, dict):
                apr.setdefault("escalation_resolved", False)
            return _gate_result(s, apr, trace)
        return {"approval": {"auto": True}, "trace": trace}

    # ---- 执行层 ----
    async def execute(self, s: SupplyChainState) -> Dict[str, Any]:
        """运单下发（真实动作）：tms-mcp.create_shipment 落盘下发，返回带时间戳回执。"""
        trace = ["execute"]  # reducer 通道：只追加本节点记录
        order = s["order"]
        plan = s.get("current_plan") or {}
        carrier = plan.get("carrier")
        o, d = _route_ends(order)
        eta_h = (s.get("eta") or {}).get("est_h")
        arrival = (datetime.now() + timedelta(hours=float(eta_h))).isoformat(timespec="minutes") \
            if eta_h else None
        calls: List[Dict[str, Any]] = []  # reducer 通道：只收本节点新增调用

        # 校验闸口闭环（S3）：规划产出未通过校验、且人工未放行 -> **不得下发**。
        # 为什么放在这里而不是只靠上游编排：`execute` 是第一个不可逆的外部动作
        # （真实落 TMS 运单），编排层一旦被改错、或人工驳回后仍被静态边推到这里，
        # 这里是最后一道防线。关闭 supervisor 时 `evaluation` 从不存在（本守卫恒不触发），
        # 所以 `off` 模式行为与改造前完全一致。
        _nh = (s.get("evaluation") or {}).get("needs_human") or {}
        if _nh and not (s.get("approval") or {}).get("approved"):
            logger.error("[execute] 规划产出未通过校验且未经人工放行，拒绝下发：%s", _nh)
            return {"trace": trace, "mcp_calls": [],
                    "order": {**order, "status": "blocked"},
                    "execution": {"ok": False, "escalation": _nh,
                                  "reason": "规划产出未通过校验，且未经人工放行（不下发）"}}

        # 人工闸口守卫（2026-10-17 收口）：**审批记录存在但未放行 -> 不得下发**。
        # 与上面 `evaluation` 守卫同源：编排层（出边）已经改成"驳回即收口"，
        # 这一条是最后一道防线——`execute` 是第一个不可逆的外部动作，任何一次
        # 编排改错或静态边误接，都会在这里被拦下而不是把驳回当放行落单。
        # 无审批记录时（非高危订单根本不经过闸口）守卫不触发，行为与改造前一致。
        _apr = s.get("approval") or {}
        if _apr and not _gate_ok(s):
            logger.error("[execute] 人工闸口未放行，拒绝下发：%s", _apr)
            return {"trace": trace, "mcp_calls": [],
                    "order": {**order, "status": "approval_rejected"},
                    "execution": {"ok": False, "approval": _apr,
                                  "reason": "人工审批未放行（不下发）"}}

        # 路径降级（起讫无法解析 / 同城 / MCP 不可用）：不下发，挂起待补录
        if plan.get("degraded"):
            return {"trace": trace, "mcp_calls": calls,
                    "order": {**order, "status": "blocked"},
                    "execution": {"ok": False,
                                  "reason": plan.get("degraded_reason")
                                  or "路径降级，运单挂起待补录（不下发）"}}
        if not carrier:
            return {"trace": trace, "mcp_calls": calls,
                    "order": {**order, "status": "pending_carrier"},
                    "execution": {"ok": False,
                                  "reason": "未匹配到运力，运单挂起待补（不下发）"}}
        r = await mcp_call("tms-mcp", "create_shipment", order_id=order["order_id"],
                           carrier_id=str(carrier.get("id") or carrier.get("name") or "unknown"),
                           eta=arrival, route=(f"{o}→{d}" if o and d else None),
                           distance_km=plan.get("distance_km"),
                           cost_est=(s.get("cost") or {}).get("est"))
        calls.append(r.to_dict())
        ok = bool(r.ok and isinstance(r.data, dict) and r.data.get("ok"))
        return {"trace": trace, "mcp_calls": calls,
                "order": {**order, "status": "dispatched" if ok else "dispatch_failed"},
                "execution": {"ok": ok, "tms_receipt": r.data if ok else None,
                              "reason": None if ok else (r.error or str(r.data))},
                "capacity": s.get("capacity")}

    async def monitor(self, s: SupplyChainState) -> Dict[str, Any]:
        """在途监控（真实采集）：并发拉 GPS 位置 + IoT 遥测 + 路线天气，运单转在途。"""
        trace = ["monitor"]  # reducer 通道：只追加本节点记录
        order = s["order"]
        route_info = s.get("route") or {}
        route_str = "→".join(str(w) for w in route_info.get("waypoints") or []) or None
        calls: List[Dict[str, Any]] = []  # reducer 通道：只收本节点新增调用

        tasks = [mcp_call("gps-mcp", "get_latest_position", order_id=order["order_id"]),
                 mcp_call("gps-mcp", "get_telemetry", order_id=order["order_id"])]
        if route_str:
            tasks.append(mcp_call("weather-mcp", "get_forecast", route=route_str))
        results = await asyncio.gather(*tasks)
        calls.extend(r.to_dict() for r in results)
        pos = results[0].data if (results[0].ok and isinstance(results[0].data, dict)) else {}
        tel = results[1].data if (results[1].ok and isinstance(results[1].data, dict)) else {}
        wx = results[2].data if (len(results) > 2 and results[2].ok
                                 and isinstance(results[2].data, dict)) else {}

        # 运单状态流转：dispatched -> in_transit（真实落盘）
        if (s.get("execution") or {}).get("ok"):
            r_st = await mcp_call("tms-mcp", "update_status",
                                  order_id=order["order_id"], status="in_transit")
            calls.append(r_st.to_dict())

        telemetry = {**tel, "position": pos}
        return {"telemetry": telemetry, "weather": wx, "mcp_calls": calls,
                "order": {**order, "status": "in_transit"
                          if (s.get("execution") or {}).get("ok") else order.get("status")},
                "trace": trace}

    async def detect_exception(self, s: SupplyChainState) -> Dict[str, Any]:
        """异常检测（规则引擎）：温控越界/车门/停留超时/ETA 超时/路线天气。"""
        trace = ["detect_exception"]  # reducer 通道：只追加本节点记录
        injected = s.get("exception") or {}
        detected = _detect_rules(s.get("order") or {}, s.get("telemetry") or {},
                                 s.get("weather") or {}, s.get("eta") or {})
        # 注入的异常（调度需求报告的事件）优先；规则引擎命中作为补充证据
        exc = injected if injected.get("code") else (detected[0] if detected else {})
        return {"exception": exc,
                "telemetry": {**(s.get("telemetry") or {}),
                              "detected": detected,
                              "detection_source": "injected" if injected.get("code")
                              else ("rule" if detected else "none")},
                "trace": trace}

    async def classify_exception(self, s: SupplyChainState) -> Dict[str, Any]:
        trace = ["classify_exception"]  # reducer 通道：只追加本节点记录
        exc = s["exception"]
        if not exc.get("name"):  # 兜底：按故障谱补全名称/子系统/自愈标记
            from sca.simulation.fault_spectrum import by_code
            ft = by_code(exc.get("code", ""))
            if ft:
                exc = {**exc, "name": ft.name, "subsystems": list(ft.subsystems),
                       "auto": ft.auto}
        # SLA 真实推导：P0/P1 需人工介入，P2/P3 自动处置
        return {"exception": {**exc, "sla": sla_target(exc.get("level", "P2"))},
                "trace": trace}

    async def retrieve_knowledge(self, s: SupplyChainState) -> Dict[str, Any]:
        trace = ["retrieve_knowledge"]  # reducer 通道：只追加本节点记录
        # 真实知识库检索（FR-10）：异常码精确命中 + 文本语义/关键词召回，返回完整 SOP
        from sca.knowledge_base import search_sop
        exc = s.get("exception") or {}
        query = " ".join(x for x in [exc.get("name", ""), exc.get("desc", ""),
                                     str(s.get("source_text") or "")[:60]]
                         if x).strip()
        hits = search_sop(query, top_k=3, code=exc.get("code", ""))
        best = hits[0] if hits else {}
        sop = {
            "sop_id": best.get("sop_id", "SOP-UNKNOWN"),
            "title": best.get("title", "通用异常处置"),
            "match": best.get("match", 0.0),
            "steps": best.get("steps", []),
            "owner": best.get("owner", ""),
            "tools": best.get("tools", []),
            "notify": best.get("notify", ""),
            "alternatives": [{"sop_id": h["sop_id"], "title": h["title"], "match": h["match"]}
                             for h in hits[1:]],
        }
        return {"sop": sop, "trace": trace}

    async def decide_action(self, s: SupplyChainState) -> Dict[str, Any]:
        trace = ["decide_action"]  # reducer 通道：只追加本节点记录
        lvl = s["exception"].get("level")
        # 高危(P0/P1)已审批通过 -> 进入重规划并结束，避免 approval_gate 死循环
        if lvl in ("P0", "P1") and s.get("approval", {}).get("approved"):
            action = "replan"
        else:
            action = route_action(lvl)        # 与决策核心一致：高危挂起、其余自动换路
        return {"route_decision": {"action": action}, "trace": trace}

    async def replan(self, s: SupplyChainState) -> Dict[str, Any]:
        """动态重规划（FR-07）：map-mcp 避行重算 + tms/gps 同步新计划；耗时 perf_counter 实测。"""
        t0 = time.perf_counter()
        trace = ["replan"]  # reducer 通道：只追加本节点记录
        new_version = int(s.get("plan_version", 1)) + 1
        old = s.get("current_plan") or {}
        order = s.get("order") or {}
        cargo = order.get("cargo") or {}
        o, d = _route_ends(order)
        cargo_type = cargo.get("type") or "普货"
        temp_ctrl = bool(order.get("temp_ctrl") or cargo_type == "冷链")
        hazmat = cargo_type == "危险品"
        carrier = (s.get("capacity") or [{}])[0] if s.get("capacity") else {}
        cost_per_km = float(carrier.get("cost_per_km", 2.6))

        exc = s.get("exception") or {}
        fault_name = exc.get("name") or exc.get("code") or "异常"
        level = exc.get("level", "P2")

        # 受影响路段（决定**是否值得**叫模型，2026-10-15 用户确认）：
        #   ① 调度员在「受影响路段（可选）」里显式录入 -> 有依据，交模型做局部避让；
        #   ② 需求原话里写了具体路况地名（高速/国道/大桥/隧道/服务区/桩号）-> 同上；
        #   ③ 两者都没有（故障谱只给异常**类型**，如"道路封闭"）-> 模型没有任何可
        #      定位信息，实测必然输出 accept，纯白等 12-30s -> allow_model=False，
        #      跳过 S2/S4/S5 直接走确定性重算，并如实说明"维持原走廊"。
        # 措辞上仍必须点明"具体路段未提供"，否则模型会读成"整条走廊封闭"而整体重画
        # 走廊（实测曾改走郑州 +67%，对一次封路避让明显失真）。
        from sca.mcp.map_server import segment_hint
        seg_input = str(s.get("affected_segment") or "").strip()
        seg_hint = seg_input or segment_hint(s.get("source_text"))
        allow_model = bool(seg_hint)
        avoid = (f"{fault_name}：{seg_hint}" if allow_model else
                 f"{fault_name}（{o}→{d} 行程中受影响，具体路段未提供）")

        # 原方案里程为 0（起讫未解析/同城）时不重规划，避免伪造"应急绕行段"假数据
        if float(old.get("distance_km") or 0) == 0.0:
            replan_ms = round((time.perf_counter() - t0) * 1000, 3)
            metrics = {**(s.get("metrics") or {})}
            metrics["replan_ms"] = replan_ms
            metrics["replan_budget_ms"] = estimate_replan_ms(level)
            metrics["within_budget"] = replan_ms <= 500.0
            return {"current_plan": {**old, "reroute": False, "skipped": True,
                                     "skip_reason": "原方案里程为0（起讫未解析/同城），不重规划"},
                    "plan_version": new_version, "route": old.get("route") or {},
                    "cost": s.get("cost") or {"est": 0, "currency": "CNY"},
                    "eta": s.get("eta") or {"est_h": None},
                    "mcp_calls": [],
                    "metrics": metrics, "trace": trace}

        calls: List[Dict[str, Any]] = []  # reducer 通道：只收本节点新增调用
        r_route = None
        if o and d:   # 起讫地缺失时不下发无效调用（需求未说明，系统不猜）
            # 对称接入 smart 版：模型据应急语境（避开受影响路段、风险厌恶、货类安全）
            # 增强重规划；模型不可用则 plan_route_smart 自动 fail-open 退回确定性重算。
            # 未给具体受影响路段时 allow_model=False：跳过三次模型调用（省 12-30s），
            # 由确定性路径给出"维持原走廊"的结论（与模型必 accept 的结果等价）。
            cargo_safety = '冷链' if temp_ctrl else '危品' if hazmat else '普货'
            if allow_model:
                src = "调度员录入" if seg_input else "需求原文识别"
                nl_req = (f"应急重规划：{o}→{d} 行程受「{fault_name}」影响，"
                          f"受影响路段为「{seg_hint}」（来源：{src}）。"
                          f"请只规避该路段实际波及的城市/走廊段，做**局部**稳妥绕行，"
                          f"不得整体更换走廊；风险厌恶偏高，保障{cargo_safety}运输安全")
            else:
                nl_req = (f"应急重规划：{o}→{d} 行程受「{fault_name}」影响（具体受影响路段未提供），"
                          f"请在**该走廊内**做局部稳妥绕行，不得整体更换走廊；风险厌恶偏高，"
                          f"保障{cargo_safety}运输安全")
            r_route = await mcp_call("map-mcp", "plan_route_smart", origin=o, dest=d,
                                     avoid=avoid, cargo_type=cargo_type, temp_ctrl=temp_ctrl,
                                     hazmat=hazmat, cost_per_km=cost_per_km,
                                     natural_lang_req=nl_req, allow_model=allow_model)
            calls.append(r_route.to_dict())
        route = r_route.data if (r_route and r_route.ok and isinstance(r_route.data, dict)
                                 and r_route.data.get("ok", True)) else None
        smart = bool(route and route.get("smart"))

        old_wp = list(old.get("waypoints", []))
        dist_old = float(old.get("distance_km") or 0)
        cost_old = float((s.get("cost") or {}).get("est") or 0)
        eta_old = float((s.get("eta") or {}).get("est_h") or 0)

        skipped_why: Optional[str] = None
        if route:
            new_wp = route.get("waypoints", old_wp)
            dist_new = float(route.get("distance_km") or dist_old)
            eta_new = float(route.get("eta_h") or eta_old)
            cost_new = float((route.get("cost") or {}).get("est") or cost_old)
            provider = route.get("provider")
            detour_km = float(route.get("detour_km") or 0)
            reroute_reason = f"map-mcp 避开「{fault_name}」重新规划"
            # 真实改道判定：模型改道被闸口驳回、或本就无实际差异时，不宣称"重规划"
            # （否则卡片会出现"绕行 +0km"这种自相矛盾的应急重规划）。
            did_reroute = abs(dist_new - dist_old) >= 1.0
            if not did_reroute:
                if route.get("model_rejected"):
                    skipped_why = ("模型提议的改道超过可接受绕行阈值，已回退原走廊"
                                   "（未产生实际改道），建议人工确认")
                elif not allow_model:
                    skipped_why = (f"未提供具体受影响路段：已跳过模型裁决，"
                                   f"维持原走廊 {dist_old}km；如需局部绕行，"
                                   f"请在「受影响路段（可选）」中补录（如：微山湖特大桥）")
                else:
                    skipped_why = ("受影响路段未映射到具体路段、且未产生实际改道，"
                                   "维持原走廊，建议人工确认")
                detour_km = 0.0
        else:  # 降级：沿用旧方案 + 保守绕行系数（不编造新路径）
            new_wp = (old_wp[:1] + ["应急绕行段"] + old_wp[1:]) if old_wp else ["应急绕行段"]
            dist_new, eta_new, cost_new = round(dist_old * 1.12, 1), round(eta_old + 1.5, 1), round(cost_old * 1.06, 1)
            provider, detour_km = "fallback", round(dist_new - dist_old, 1)
            why = (r_route.error if r_route else "需求未提供起运地/目的地") or "unknown"
            reroute_reason = f"无法调用 map-mcp 重规划，按保守绕行系数估算（{why}）"
            did_reroute = True

        new_plan = {**old, "version": new_version, "reroute": did_reroute,
                    "waypoints": new_wp, "distance_km": dist_new,
                    "provider": provider, "avoid": avoid,
                    "reroute_reason": reroute_reason,
                    "previous": {"waypoints": old_wp, "distance_km": dist_old,
                                 "cost": cost_old, "eta_h": eta_old}}
        # 重规划自身的 notes（模型改道 / 绕行判定 / 跳过模型的原因）必须带到前端：
        # 否则卡片只剩一个里程数字，调度员看不出"为什么维持原走廊"（2026-10-15）
        if route and route.get("notes"):
            new_plan["notes"] = list(route["notes"])
        if skipped_why:
            new_plan["skipped"] = True
            new_plan["skip_reason"] = skipped_why
        if smart:
            new_plan["model_constraints"] = route.get("model_constraints")
            new_plan["model_decision"] = route.get("model_decision")
            new_plan["summary"] = route.get("summary")
            new_plan["smart"] = True

        # 新计划同步：TMS 运单改道（保留历史版本）+ GPS 路线重下发
        if o and d:
            r_tp = await mcp_call("tms-mcp", "update_plan", order_id=order.get("order_id"),
                                  route=f"{o}→{d}", distance_km=dist_new,
                                  eta=(datetime.now() + timedelta(hours=eta_new))
                                  .isoformat(timespec="minutes") if eta_new else None,
                                  cost_est=cost_new, reason=avoid)
            calls.append(r_tp.to_dict())
            r_sr = await mcp_call("gps-mcp", "set_route", order_id=order.get("order_id"),
                                  origin=o, dest=d, waypoints=new_wp, distance_km=dist_new)
            calls.append(r_sr.to_dict())

        # 实测耗时（perf_counter）；预算口径单独保留，供 L0 <500ms 对比
        replan_ms = round((time.perf_counter() - t0) * 1000, 3)
        metrics = {**(s.get("metrics") or {})}
        metrics["replan_ms"] = replan_ms
        metrics["replan_budget_ms"] = estimate_replan_ms(level)
        metrics["within_budget"] = replan_ms <= 500.0
        return {"current_plan": new_plan, "plan_version": new_version,
                "route": {"waypoints": new_wp, "distance_km": dist_new,
                          "origin": o, "dest": d, "provider": provider,
                          "detour_km": detour_km,
                          **({"model_constraints": route.get("model_constraints"),
                              "model_decision": route.get("model_decision"),
                              "summary": route.get("summary"), "smart": True}
                             if smart else {})},
                "cost": {"est": cost_new, "currency": "CNY"},
                "eta": {"est_h": eta_new}, "mcp_calls": calls,
                "metrics": metrics, "trace": trace}

    async def notify(self, s: SupplyChainState) -> Dict[str, Any]:
        """结果通知（真实动作）：先组装给司机/收货方的调度通知，经调度员审核闸口后，
        再经 notify-mcp 统一通道推送（钉钉 webhook 已配置时推司机/收货方；否则本机
        Windows 桌面弹窗；审计日志始终落盘）。

        调度员审核（dispatch_review）：调度结果必须经调度员确认无误，才对司机与收货方
        发送钉钉通知。未开启审核或审核被驳回时，不下发外部通知（仅留痕）。

        两种场景共用本节点：有异常 -> "异常处置完成"；无异常（常规调度）-> 卡片不再
        名为"处置完成"，改称"运输方案下发确认（无异常上报）"，避免把常规单说成异常单。
        """
        trace = ["notify"]  # reducer 通道：只追加本节点记录
        exc = s.get("exception") or {}
        plan = s.get("current_plan") or {}
        order = s.get("order") or {}
        o, d = _route_ends(order)
        prev = plan.get("previous") or {}
        sop = s.get("sop") or {}
        carrier = plan.get("carrier") or {}
        has_exc = bool(exc.get("code"))
        level = exc.get("level") or ("P3" if not has_exc else "P2")

        fault_name = exc.get("name") or exc.get("code") or "异常"
        eta_h = (s.get("eta") or {}).get("est_h")

        # —— 对外通知正文（司机 / 收货方）：只讲业务 ——
        # 不含 SOP 编号、模型裁决、实测耗时、系统内部补录提示等（2026-10-15 用户反馈：
        # "未提供具体受影响路段…请在「受影响路段（可选）」中补录"属系统操作提示，
        # 不适合暴露给司机和收货方）。内部信息改放 detail，仅在调度员审核卡展示。
        head = (f"### 【{level}】{fault_name} 处置完成" if has_exc
                else "### 【常规调度】运输方案下发确认（需求未上报异常）")
        md = [head,
              f"- **订单**：{order.get('order_id')}"
              f"（{o or '?'} → {d or '?'}，{((order.get('cargo') or {}).get('type')) or '普货'}）",
              f"- **承运商**：{carrier.get('name') or '未匹配'}"]
        # 完整道路规划：起讫 + 沿途途经点逐段串出，司机照此行驶
        wp = [str(w).strip() for w in (plan.get("waypoints") or []) if str(w or "").strip()]
        md.append(f"- **行驶路线**：{' → '.join(wp)}" if len(wp) >= 2
                  else "- **行驶路线**：待补录（起运地 / 目的地未解析）")
        _dist = plan.get("distance_km")
        _line = f"- **全程**：{_dist if _dist is not None else '?'}km"
        if isinstance(eta_h, (int, float)) and eta_h:
            _line += f"，预计 {round(float(eta_h), 1)} 小时"
        md.append(_line)
        # 调度调整（对外口径）：只说"是否改道 + 为什么"，不复述内部裁决过程
        if has_exc and plan.get("reroute"):
            _d0, _d1 = prev.get("distance_km"), plan.get("distance_km")
            _extra = (f"，较原计划 +{round(float(_d1) - float(_d0), 1)}km"
                      if isinstance(_d0, (int, float)) and isinstance(_d1, (int, float))
                      and float(_d1) > float(_d0) else "")
            md.append(f"- **调度调整**：因「{fault_name}」避让受影响路段，"
                      f"行驶路线已局部调整{_extra}")
        elif has_exc:
            md.append(f"- **调度调整**：无需改道——本次「{fault_name}」已处置完毕，"
                      f"行驶路线维持原规划")
        else:
            md.append("- **调度调整**：无异常上报，按规划路线正常行驶")
        if isinstance(eta_h, (int, float)) and eta_h:
            md.append("- **预计送达**："
                      + (datetime.now() + timedelta(hours=float(eta_h))).strftime("%m-%d %H:%M"))
        text = "\n".join(md)

        # —— 调度员内部信息（只随审核卡展示，不随通知下发）——
        dt: List[str] = []
        if has_exc and sop.get("sop_id"):
            dt.append(f"SOP：{sop.get('sop_id')}（{sop.get('title')}）")
        if has_exc:
            if plan.get("reroute"):
                dt.append(f"路线决策：已改道 {prev.get('distance_km', '?')}km → "
                          f"{plan.get('distance_km', '?')}km")
            else:
                dt.append("路线决策：维持原走廊（未产生实际改道）")
            if plan.get("skip_reason"):
                dt.append(f"未改道原因：{plan.get('skip_reason')}")
            mdec = plan.get("model_decision") or {}
            if mdec:
                dt.append("模型裁决：" + str(mdec.get("source") or "?")
                          + (f"（{mdec.get('action')}）" if mdec.get("action") else ""))
            dt.append(f"重规划耗时：{(s.get('metrics') or {}).get('replan_ms', '?')}ms"
                      f"（规则快路径目标 ≤500ms）")
        detail = "\n".join(dt)

        title = (f"【{level}】{fault_name}处置通知 · 请司机与收货方查收" if has_exc
                 else "【常规调度】运输方案下发确认 · 请司机与收货方查收")
        # 收件人 = 角色 + 手机号（钉钉群内 @ 到本人用）。号码为空则只推送不 @ ——
        # 手机号是真实个人信息，缺失时留空并在回执里标明，绝不自动生成。
        targets = [
            {"role": "司机", "phone": _valid_phone(carrier.get("driver_phone"))},
            {"role": "收货方", "phone": _valid_phone(order.get("contact"))},
        ]
        review_targets = [{"role": t["role"], "phone_masked": _mask_phone(t["phone"]),
                           "at": bool(t["phone"])} for t in targets]

        # —— 调度员审核闸口 ——
        review = {"status": "pending", "title": title, "content": text,
                  "detail": detail, "targets": review_targets, "ts": int(time.time())}
        if settings.dispatch_review_enabled:
            decision = interrupt({
                "type": "dispatch_review",
                "title": title,
                "content": text,
                "detail": detail,
                "summary": f"{order.get('order_id')} · {o or '?'}→{d or '?'} · "
                           f"{carrier.get('name') or '未匹配'} · "
                           f"将 @ {'、'.join(t['role'] for t in targets if t['phone']) or '无人（未配置手机号）'}",
                "targets": review_targets,
            })
            review["decision"] = decision
            if not (isinstance(decision, dict) and decision.get("approved")):
                review["status"] = "rejected"
                review["reason"] = (decision or {}).get("reason", "调度员驳回") \
                    if isinstance(decision, dict) else "调度员驳回"
                logger.info("[notify] 调度员驳回下发，未向司机/收货方推送")
                return {"dispatch_review": review, "trace": trace,
                        "notifications": []}

        # —— 审核通过（或审核关闭）：向司机与收货方真实下发 ——
        notifications: List[Dict[str, Any]] = []  # reducer 通道：只收本节点新增通知
        calls: List[Dict[str, Any]] = []  # reducer 通道：只收本节点新增调用
        sent_any = False
        for tgt in targets:
            at = [tgt["phone"]] if tgt["phone"] else []
            r = await mcp_call("notify-mcp", "send_dispatch_notice", target=tgt["role"],
                               text=text, title=title, at_mobiles=at)
            calls.append(r.to_dict())
            data = r.data if isinstance(r.data, dict) else {}
            ok = bool(r.ok and data.get("sent"))
            sent_any = sent_any or ok
            notifications.append({"channel": data.get("channel") or "unknown",
                                  "target": tgt["role"], "at": bool(at),
                                  "at_mobiles_masked": ([_mask_phone(tgt["phone"])]
                                                        if tgt["phone"] else []),
                                  "title": title, "sent": ok, "detail": data,
                                  "ts": int(time.time())})
        review["status"] = "approved" if sent_any else "sent_failed"
        # review 是本地展示对象（不是状态通道），保持"累计全部通知"的原语义
        review["notifications"] = list(s.get("notifications") or []) + notifications
        logger.info("[notify] 调度员审核通过，已向司机/收货方推送（sent_any=%s）", sent_any)
        return {"dispatch_review": review, "notifications": notifications,
                "mcp_calls": calls, "trace": trace}

    # ---- 反思层 ----
    async def reflect(self, s: SupplyChainState) -> Dict[str, Any]:
        """复盘（真实指标）：新旧方案对比、处置动作、SOP 命中、通知结果。"""
        trace = ["reflect"]  # reducer 通道：只追加本节点记录
        m = {**(s.get("metrics") or {})}
        plan = s.get("current_plan") or {}
        prev = plan.get("previous") or {}
        has_exc = bool((s.get("exception") or {}).get("code"))
        m["handled"] = has_exc
        # 无异常（常规调度）时没有"改道前后"可比，增量为 0 而非整段里程
        def _delta(now: float, before: Any) -> float:
            if not has_exc or not isinstance(before, (int, float)):
                return 0.0
            return round(float(now or 0) - float(before), 1)
        m["review"] = {
            "order_id": s.get("order_id"),
            "level": (s.get("exception") or {}).get("level"),
            "action": (s.get("route_decision") or {}).get("action"),
            "plan_version": plan.get("version"),
            "detour_km": 0.0 if not has_exc else _delta(plan.get("distance_km"),
                                                       prev.get("distance_km")),
            "cost_delta": _delta((s.get("cost") or {}).get("est"), prev.get("cost")),
            "eta_delta_h": _delta((s.get("eta") or {}).get("est_h"), prev.get("eta_h")),
            "sop_id": (s.get("sop") or {}).get("sop_id"),
            "notified": bool((s.get("notifications") or [{}])[-1].get("sent"))
                        if s.get("notifications") else False,
        }
        return {"metrics": m, "trace": trace}

    async def update_knowledge(self, s: SupplyChainState) -> Dict[str, Any]:
        """知识沉淀（真实动作）：案例落盘 data/cases.json（FR-09）。"""
        trace = ["update_knowledge"]  # reducer 通道：只追加本节点记录
        review = (s.get("metrics") or {}).get("review") or {}
        exc = s.get("exception") or {}
        case = {
            "case_id": f"CASE-{int(time.time() * 1000)}-{s.get('order_id')}",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "order_id": s.get("order_id"),
            "source_text": str(s.get("source_text") or "")[:120],
            "level": exc.get("level"), "fault_code": exc.get("code"),
            "fault_name": exc.get("name"),
            "detection_source": (s.get("telemetry") or {}).get("detection_source"),
            "action": review.get("action"), "sop_id": review.get("sop_id"),
            "plan_version": review.get("plan_version"),
            "detour_km": review.get("detour_km"), "cost_delta": review.get("cost_delta"),
            "eta_delta_h": review.get("eta_delta_h"),
            "replan_ms": (s.get("metrics") or {}).get("replan_ms"),
            "notified": review.get("notified"),
        }
        case_id = _append_case(case)
        return {"knowledge_updated": {"case_id": case_id, "cases_file": str(_CASES_FILE)}
                if case_id else {"case_id": None, "error": "落盘失败（见日志）"},
                "trace": trace}

    async def end(self, s: SupplyChainState) -> Dict[str, Any]:
        trace = ["end"]  # reducer 通道：只追加本节点记录
        return {"trace": trace}

    # ---- 编译 ----
    def compile(self, checkpointer=None, interrupt_before=None):
        """按 `SCA_SUPERVISOR_MODE` 建图。

        `off`（默认）—— 改造前那张图，逐边保持原样，零行为变更；
        `shadow` / `on` —— 规划段交给 supervisor。两种模式**拓扑完全相同**，只有
        `supervisor` 节点的路由来源不同（shadow 走 `legacy_route()` 复刻原拓扑，
        on 走 `decide()`）。拓扑必须一致，否则影子模式观测到的就不是同一件事。
        """
        mode = (getattr(settings, "supervisor_mode", "off") or "off").strip().lower()
        supervised = mode in ("shadow", "on")

        g = StateGraph(SupplyChainState)
        nodes = [
            "receive_order", "load_context", "intent_route",
            "plan_prepare", "agent_route", "agent_precheck", "plan_arbiter",
            "approval_gate",
            "execute", "monitor", "detect_exception", "classify_exception",
            "retrieve_knowledge", "decide_action", "replan", "notify",
            "reflect", "update_knowledge", "end",
        ]
        if supervised:
            nodes += ["supervisor", "plan_evaluator"]
        for n in nodes:
            g.add_node(n, getattr(self, n))

        g.set_entry_point("receive_order")
        g.add_edge("receive_order", "load_context")
        g.add_edge("load_context", "intent_route")

        if supervised:
            # 混合路由：出去动态（supervisor 返回 Command(goto=...)），回来静态
            # （worker -> evaluator -> supervisor）。LangGraph 的自动扇入只对静态边
            # 生效，所以"回来"这一段必须是静态边，否则并行分支的汇聚无从保证。
            g.add_edge("intent_route", "supervisor")
            for w in ("plan_prepare", "agent_route", "agent_precheck", "plan_arbiter"):
                g.add_edge(w, "plan_evaluator")
            g.add_edge("plan_evaluator", "supervisor")
            # ⚠️ `approval_gate` 的出边**不接回 supervisor**，而是走下面那条共用的
            # 条件边（放行 -> execute / 驳回 -> end）。原因：这个节点有两个身份——
            # 规划段的审批闸口（本轮接管范围），以及异常处置段
            # `decide_action -> approval_gate` 的二次审批（**不在**本轮范围内）。若把
            # 出边改接 supervisor，处置段那条路径会一起被改道，`on` 就与 `off` 产生
            # 真实行为差异（实测：p1 场景会跳过"重复下发"那一步），竖切片的等价性
            # 验证随之失效。
            # ⚠️ supervisor **不声明任何静态出边**：实测官方引擎下 `Command.goto` 与静态
            # 出边是**叠加**而非覆盖（static_plus_goto 场景得到 4 个目标），一旦声明，
            # Command 就失去独占路由权，规划段会被同时派往两个方向。
        else:
            # ---- 规划段：并行扇出 -> 汇聚 ----
            # 一个节点挂多条出边 = 这些目标在**同一超步并行执行**（LangGraph 原生语义）；
            # 汇聚点（plan_arbiter）自动等待两个分支全部到达。为什么只有这两个分支能并行：
            # 路线规划与前置取证互不依赖；而运力匹配依赖路线的 distance_km、成本精算依赖
            # 选中承运商的 cost_per_km，属链式依赖，只能留在汇聚点按序求解。
            g.add_edge("intent_route", "plan_prepare")
            g.add_edge("plan_prepare", "agent_route")
            g.add_edge("plan_prepare", "agent_precheck")
            g.add_edge("agent_route", "plan_arbiter")
            g.add_edge("agent_precheck", "plan_arbiter")
            # 规划层：高危 -> 审批挂起，否则直接执行
            g.add_conditional_edges(
                "plan_arbiter",
                lambda s: "approval_gate" if _needs_approval(s) else "execute",
                {"approval_gate": "approval_gate", "execute": "execute"},
            )

        # ---- 人工闸口出边（两种模式共用）----
        # 放行（auto / approved）才下发；**驳回一律收口到 end，不下发**。
        # 放在 if/else 外面是刻意的：这是业务不变量，不是编排策略，不允许 `off` 与
        # `on` 各有一套。改造前 `off` 用静态边 `-> execute`，把"调度员驳回"当成了
        # 放行——这是本轮修掉的松口。
        g.add_conditional_edges(
            "approval_gate",
            _after_gate,
            {"execute": "execute", "end": "end"},
        )

        g.add_edge("execute", "monitor")
        g.add_edge("monitor", "detect_exception")
        # 执行层：有异常 -> 异常处置链；无异常（常规调度）-> 直接进下发审核，
        # 不做异常分级/知识检索/应急重规划（没有异常可处置，不硬凑流程）
        g.add_conditional_edges(
            "detect_exception",
            lambda s: "classify_exception" if s.get("exception") else "notify",
            {"classify_exception": "classify_exception", "notify": "notify"},
        )
        g.add_edge("classify_exception", "retrieve_knowledge")
        g.add_edge("retrieve_knowledge", "decide_action")
        # 处置：重规划 or 二次审批
        g.add_conditional_edges(
            "decide_action",
            lambda s: "replan" if s["route_decision"]["action"] == "replan" else "approval_gate",
            {"replan": "replan", "approval_gate": "approval_gate"},
        )
        g.add_edge("replan", "notify")
        g.add_edge("notify", "reflect")
        g.add_edge("reflect", "update_knowledge")
        g.add_edge("update_knowledge", "end")
        g.set_finish_point("end")

        cp = checkpointer
        if cp is None:
            from sca.storage import get_checkpointer
            cp = get_checkpointer()
        return g.compile(checkpointer=cp, interrupt_before=interrupt_before or [])
