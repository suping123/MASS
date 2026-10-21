"""后端 API 层（FR-13）：纯 JSON REST，与前端完全分离。

设计要点（前后端分离）：
  * 所有 JSON 接口统一挂在 SCA_API_PREFIX（默认 /api）下，例如 /api/route；
  * 前端是独立静态站点（frontend/），由本服务在 "/" 挂载托管，也可单独用任意
    静态服务器托管（靠 CORS 跨域调用 API，SCA_CORS_ORIGINS 控制来源）；
  * 业务方只需改 .env 即可调整 host/port/前缀/跨域/前端目录/HITL 开关等。

运行（需 fastapi+uvicorn；Windows + PG 检查点必须显式指定 Selector 事件循环，
否则 psycopg 异步驱动连不上库，详见 sca/win_loop.py 与 README）：
  uvicorn app.main:app --port 8000 --loop sca.win_loop:win_selector_loop_factory
零依赖回退（未装 fastapi 时）：
  python -m sca.api                                  # 标准库 http.server，同样 /api 前缀 + 托管前端
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from sca._compat import Command, GraphInterrupt, GraphRecursionError
from sca.util.aio import run_blocking
from sca.settings import settings

logger = logging.getLogger("sca.api")

# Windows + psycopg 异步驱动需要 SelectorEventLoop（默认 Proactor 不兼容）。
# uvicorn 自身已切 Selector 策略；此处兜底覆盖直接 asyncio.run 的脚本/测试场景。
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:  # noqa
        pass

# HITL 挂起线程池：thread_id -> 已编译图（含内存检查点），供 /api/resume 恢复
_PENDING: Dict[str, Any] = {}

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    HAVE_FASTAPI = True
except ImportError:  # 零依赖回退
    HAVE_FASTAPI = False
    FastAPI = HTTPException = JSONResponse = None


# ---------------------------------------------------------------------------
# 路由处理函数（与具体 Web 框架解耦，fastapi / 标准库共用）
# ---------------------------------------------------------------------------
async def h_health() -> Dict[str, Any]:
    return {"status": "ok", "hitl_enabled": settings.hitl_enabled,
            "model": settings.intent_model,
            "model_available": bool(settings.dashscope_api_key)}


async def h_eval() -> Dict:
    from sca.eval.harness import run_all
    rep = await run_all()
    return {
        "metrics": [m.__dict__ for m in rep["metrics"]],
        "raw": {k: v for k, v in rep["raw"].items()
                if not isinstance(v, dict)},
    }


async def h_dashboard_data() -> Dict:
    """一次性返回看板所需数据（前端单页加载用）。"""
    ev = await h_eval()
    return {"eval": ev}


# ---------------------------------------------------------------------------
# 承运商（运力）配置（前端"承运商"模块用）
# ---------------------------------------------------------------------------
async def h_carriers_list() -> Dict:
    from sca.mcp.carriers_store import load_carriers
    return {"carriers": load_carriers()}


async def h_carriers_upsert(payload: Dict[str, Any]) -> Dict:
    if not isinstance(payload, dict) or not payload.get("name"):
        return {"ok": False, "error": "承运商 name 必填"}
    from sca.mcp.carriers_store import upsert_carrier
    from sca.mcp.notify_server import valid_phone
    phone = (payload.get("driver_phone") or "").strip()
    if phone and not valid_phone(phone):
        return {"ok": False, "error": "司机手机号格式不正确（应为 11 位中国大陆手机号）"}
    rec = upsert_carrier(payload)
    return {"ok": True, "carrier": rec}


async def h_carriers_delete(payload: Dict[str, Any]) -> Dict:
    cid = payload.get("id") or (payload.get("carrier_id"))
    if not cid:
        return {"ok": False, "error": "缺少承运商 id"}
    from sca.mcp.carriers_store import delete_carrier
    ok = delete_carrier(str(cid))
    return {"ok": ok, "id": str(cid),
            "message": "已删除" if ok else "未找到该承运商"}


def _interrupt_payload(result: Any) -> Any:
    """从 LangGraph 中断结果里取出 interrupt(...) 的载荷（兼容官方与零依赖垫片）。

    - 官方：result["__interrupt__"] 是 Interrupt 元组，载荷在 .value；
    - 垫片：GraphInterrupt 携带 payload。
    """
    if isinstance(result, dict):
        intr = result.get("__interrupt__")
        if intr:
            first = intr[0] if isinstance(intr, (list, tuple)) else intr
            return getattr(first, "value", None)
    return None


def _needs_approval_from(interrupt_value: Any) -> bool:
    """调度员下发审核（dispatch_review）不是"高危审批"，不应复用审批横幅。"""
    if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "dispatch_review":
        return False
    return True


_SEGMENT_KEYS = ("affected_segment", "segment", "affected_road", "affected_section")


def _read_segment(payload: Optional[Dict[str, Any]]) -> str:
    """读取「受影响路段（可选）」：应急重规划时模型做局部避让的依据。

    是**可选**输入（故障谱只给异常类型、不含路段桩号），故不做格式校验、不阻断；
    留空时 replan 会跳过模型裁决、维持原走廊（省 12-30s）。
    """
    p = payload or {}
    for k in _SEGMENT_KEYS:
        v = str(p.get(k) or "").strip()
        if v:
            return v[:120]
    return ""


_ORDER_KEYS = ("order_id", "order_no", "shipment_id", "waybill_no")


def _read_order_id(payload: Optional[Dict[str, Any]]) -> str:
    """读取调度员在「异常描述」区录入的**订单号**（关键信息）。

    填了就以它为准：值会与「承运商管理」里的"承运订单号"对应，capacity-mcp 据此
    优先沿用该运单的在途承运商（异常处置不因重算而换车换司机）；留空则由需求文本
    里的单号或系统生成的单号决定。

    只接受字母/数字/-/_（订单号会原样进入钉钉通知与落盘，不做宽松放行）。
    """
    p = payload or {}
    for k in _ORDER_KEYS:
        v = str(p.get(k) or "").strip()
        if v:
            return re.sub(r"[^A-Za-z0-9\-_]", "", v)[:40]
    return ""


def _scheduling_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """把状态机最终 state 整理成"调度产出"报文（真实数据，非仅轨迹）。"""
    return {
        "trace": result.get("trace"),
        "replan_ms": (result.get("metrics") or {}).get("replan_ms"),
        "replan_budget_ms": (result.get("metrics") or {}).get("replan_budget_ms"),
        "within_budget": (result.get("metrics") or {}).get("within_budget"),
        "review": (result.get("metrics") or {}).get("review"),
        "plan_version": result.get("plan_version"),
        "sop": result.get("sop"),
        "order": result.get("order"),
        "exception": result.get("exception"),
        "intent": result.get("intent"),
        "capacity": result.get("capacity"),
        "plan": result.get("current_plan"),
        "route": result.get("route"),
        "cost": result.get("cost"),
        "eta": result.get("eta"),
        "route_decision": result.get("route_decision"),
        "telemetry": result.get("telemetry"),
        "weather": result.get("weather"),
        "notifications": result.get("notifications"),
        "dispatch_review": result.get("dispatch_review"),
        # 审批结论也必须回传：`_final_status()` 靠 `approval.approved is False` 判「驳回」。
        # 少了这一项，高危审批被驳回的单子会以 `done` 收尾——等于把驳回记成了成功。
        "approval": result.get("approval"),
        "knowledge_updated": result.get("knowledge_updated"),
        "affected_segment": result.get("affected_segment") or None,  # 可选录入的避让依据
        "mcp_calls": result.get("mcp_calls"),       # 真实 MCP 调用证据
    }


async def _state_values(graph: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """读取挂起中图实例的当前 state values（异步接口：本项目检查点全走 ainvoke）。

    为什么需要：HITL 挂起（审批 / 下发审核）发生在状态机跑完规划与运力匹配之后，
    若挂起响应只回一句"已挂起"，调度员在审批前看不到解析结果/路线/运力，
    无法核对系统是否理解正确（2026-10-15 用户反馈"看不到结果"）。这里把已算好的
    快照一并回传，前端即可在审批前展示。读取失败只 warn，不阻断挂起响应。
    """
    try:
        st = await graph.aget_state(cfg)
    except Exception as exc:  # noqa: 快照只是增强，失败照常挂起
        logger.warning("[api] 读取挂起 state 失败（不影响挂起）：%s", exc)
        return {}
    vals = getattr(st, "values", None) if st is not None else None
    if vals is None and isinstance(st, dict):
        vals = st.get("values")
    return vals if isinstance(vals, dict) else {}


# ---------------------------------------------------------------------------
# 会话历史埋点（把 h_trigger / h_resume 的多轮交互串成一条可回放的时间线）
# ---------------------------------------------------------------------------
def _sessions():
    """惰性导入 sca.sessions；模块不可用时返回 None（埋点失败不得阻断业务）。"""
    try:
        from sca import sessions
        return sessions
    except Exception as exc:  # noqa: 埋点模块异常 → 主流程照常跑
        logger.warning("[api] 会话历史模块不可用：%s", exc)
        return None


def _session_start(thread_id: str, **kw) -> bool:
    s = _sessions()
    if not s:
        return False
    try:
        s.start(thread_id, source="api", **kw)
        return True
    except Exception as exc:
        logger.warning("[api] 会话 %s 开启记录失败：%s", thread_id, exc)
        return False


def _session_event(thread_id: str, etype: str, label: str, actor: str = "system",
                   detail: Optional[Dict[str, Any]] = None) -> bool:
    s = _sessions()
    if not s:
        return False
    try:
        return s.event(thread_id, etype, label, actor=actor, detail=detail) is not None
    except Exception as exc:
        logger.warning("[api] 会话 %s 事件写入失败：%s", thread_id, exc)
        return False


def _session_blocked(thread_id: str, text: str, reason: str,
                     missing: Optional[List[str]] = None) -> bool:
    """受理被阻断也留痕：事后能查到"这个需求为什么没被受理"。"""
    s = _sessions()
    if not s:
        return False
    try:
        s.start(thread_id, source="api", text=text)
        s.event(thread_id, "blocked", reason, actor="系统",
                detail={"missing": missing or []})
        s.finish(thread_id, s.STATUS_BLOCKED, error=reason)
        return True
    except Exception as exc:
        logger.warning("[api] 会话 %s 阻断记录失败：%s", thread_id, exc)
        return False


def _session_hold(thread_id: str, level: str, interrupt_value: Any) -> bool:
    """挂起：区分「高危待审批」与「调度结果待审核下发」两种等待态。"""
    s = _sessions()
    if not s:
        return False
    try:
        is_review = isinstance(interrupt_value, dict) and \
            interrupt_value.get("type") == "dispatch_review"
        status = s.STATUS_REVIEW if is_review else s.STATUS_APPROVAL
        label = "挂起：调度结果等待调度员审核后下发" if is_review \
            else (f"挂起：{level} 高危，等待人工审批" if level else "挂起：等待人工审批")
        s.event(thread_id, "interrupt", label, actor="系统",
                detail={"type": (interrupt_value or {}).get("type") if isinstance(interrupt_value, dict) else None,
                        "level": level})
        s.patch(thread_id, status=status, interrupt=interrupt_value)
        return True
    except Exception as exc:
        logger.warning("[api] 会话 %s 挂起记录失败：%s", thread_id, exc)
        return False


def _final_status(payload: Dict[str, Any]) -> str:
    """从调度产出判定会话终态：驳回 或 完成。"""
    s = _sessions()
    done = s.STATUS_DONE if s else "done"
    rejected = s.STATUS_REJECTED if s else "rejected"
    dr = payload.get("dispatch_review") or {}
    if isinstance(dr, dict) and dr.get("status") == "rejected":
        return rejected
    ap = payload.get("approval") or {}
    if isinstance(ap, dict) and ap.get("approved") is False:
        return rejected
    return done


def _session_finish(thread_id: str, payload: Dict[str, Any], error: Optional[str] = None) -> bool:
    """收尾：先把通知回执逐条记进时间线，再落终态与摘要。"""
    s = _sessions()
    if not s:
        return False
    try:
        for n in (payload.get("notifications") or []):
            if not isinstance(n, dict):
                continue
            s.event(thread_id, "notify",
                    f"通知{n.get('target') or '?'}：{'已送达' if n.get('sent') else '未送达'}",
                    actor="系统",
                    detail={"channel": n.get("channel"), "sent": n.get("sent"),
                            "at_mobiles_masked": n.get("at_mobiles_masked"),
                            "reason": n.get("reason") or n.get("error")})
        status = s.STATUS_ERROR if error else _final_status(payload)
        s.finish(thread_id, status, result=None if error else payload, error=error)
        return True
    except Exception as exc:
        logger.warning("[api] 会话 %s 收尾失败：%s", thread_id, exc)
        return False


async def h_sessions_list(params: Dict[str, Any]) -> Dict:
    """会话历史列表（状态/等级/关键词筛选 + 分页）。"""
    s = _sessions()
    if not s:
        return {"total": 0, "sessions": [], "error": "会话历史模块不可用"}
    p = params or {}
    try:
        return s.list_sessions(status=str(p.get("status") or ""),
                               level=str(p.get("level") or ""),
                               q=str(p.get("q") or ""),
                               limit=int(p.get("limit") or 50),
                               offset=int(p.get("offset") or 0))
    except Exception as exc:
        return {"total": 0, "sessions": [], "error": str(exc)}


async def h_sessions_detail(sid: str) -> Dict:
    s = _sessions()
    if not s:
        return {"error": "会话历史模块不可用"}
    rec = s.get_session(str(sid or "").strip())
    if rec is None:
        return {"error": f"未找到会话 {sid}"}
    rec["status_label"] = s.STATUS_LABEL.get(rec.get("status"), rec.get("status"))
    return {"session": rec}


async def h_sessions_stats() -> Dict:
    s = _sessions()
    if not s:
        return {"total": 0, "error": "会话历史模块不可用"}
    try:
        return s.stats()
    except Exception as exc:
        return {"total": 0, "error": str(exc)}


async def h_sessions_delete(payload: Dict[str, Any]) -> Dict:
    s = _sessions()
    sid = str((payload or {}).get("id") or "").strip()
    if not s:
        return {"ok": False, "error": "会话历史模块不可用"}
    if not sid:
        return {"ok": False, "error": "缺少会话 id"}
    return s.delete_session(sid)


async def h_sessions_clear(payload: Dict[str, Any]) -> Dict:
    """清空历史：需显式 confirm=CLEAR（危险操作闸口），且先归档再清。"""
    s = _sessions()
    if not s:
        return {"ok": False, "error": "会话历史模块不可用"}
    return s.clear_sessions(str((payload or {}).get("confirm") or ""))


async def h_parse(payload: Dict[str, Any]) -> Dict:
    """调度需求受理：自然语言 -> 结构化运单 + 缺失字段提示（FR-01）。"""
    from sca.intake import parse_dispatch_request
    # parse_dispatch_request 是同步函数，规则解析不出起讫地时会**发模型 HTTP 请求**；
    # 直接调用会把事件循环卡住整个超时窗口。挪到线程池执行。
    return await run_blocking(parse_dispatch_request,
                              str(payload.get("text", "") or "").strip(),
                              use_model=bool(payload.get("use_model", True)))


def _anomaly_from_code(code: str) -> Dict[str, Any]:
    """按故障码重建完整异常对象（用于「历史会话继续处理」时复用原异常）。

    未命中故障谱时返回确定性的「未识别异常」，绝不随机采样一个异常冒充原异常。
    """
    if code and code != "E-UNKNOWN":
        try:
            from sca.simulation import FaultInjector
            a = FaultInjector().inject_code(code)
            if a:
                return a
        except Exception as exc:  # noqa  # 故障码失效（谱表变更）时降级为未识别
            logger.info("[api] 故障码 %s 重建失败，按未识别异常处理：%s", code, exc)
    return {"code": "E-UNKNOWN", "name": "未识别异常", "level": "P2",
            "desc": "系统未能在故障谱中匹配该描述，按默认 P2 记录并提示人工确认",
            "subsystems": [], "auto": False, "source": "unclassified",
            "ts": int(time.time())}


async def _dispatch_flow(*, thread_id: str, text: str, order: Dict[str, Any],
                         anomaly: Optional[Dict[str, Any]], level_source: str,
                         level_evidence: Optional[Dict[str, Any]],
                         order_source: str, order_missing: Optional[List[str]] = None,
                         receiver_phone: str = "", user_id: str = "",
                         level: Optional[str] = None,
                         parse_notes: Optional[List[str]] = None,
                         affected_segment: str = "") -> Dict:
    """执行一次完整的调度/异常处置：建图 → 受理埋点 → 注入遥测 → 跑状态机 → HITL 挂起/收尾。

    为什么抽出来：前端「从历史会话重新处理」与 /trigger 必须走**同一条路径**——
    重处理只是换了一份输入（原快照或调度员修订后的文本），thread_id 不变；
    后续的状态机、HITL 挂起、会话埋点若各走各的，两边会各自漂移
    （一边加挂起处理、另一边漏掉）。

    anomaly 为 None 表示"无异常上报"（常规调度，P3）：状态机 监控后 直接进下发审核，
    不进异常分级/知识检索/应急重规划。此时 level 由调用方显式传入（默认 P3）。
    """
    from sca.agents import SupplyChainGraph
    from sca.intent import build_intent_model

    level = level or (anomaly or {}).get("level") or ("P3" if anomaly is None else "P2")
    g = SupplyChainGraph(intent_model=build_intent_model())
    graph = g.compile()
    if receiver_phone:
        order = {**order, "contact": receiver_phone}
    # recursion_limit 必须显式设置：官方默认 10007（≈无上限），垫片读同一环境变量。
    # 引入 supervisor 循环后，"没有上限"就等于"死循环风险"。
    cfg = {"configurable": {"thread_id": thread_id},
           "recursion_limit": settings.recursion_limit}
    # 会话历史：开启记录 + 记录"受理"事件（原文与手机号均已脱敏后落盘）
    _session_start(thread_id, text=text, order=order, level=level,
                   level_source=level_source, level_evidence=level_evidence,
                   exception=anomaly, receiver_phone=receiver_phone,
                   affected_segment=affected_segment)
    _session_event(thread_id, "trigger", f"受理需求（订单来源：{order_source or '未知'}）",
                   actor="调度员",
                   detail={"origin": (order or {}).get("origin"),
                           "dest": (order or {}).get("dest"),
                           "cargo": (order or {}).get("cargo"),
                           "level_source": level_source})

    # 长短期记忆（fail-open）：先观察本轮输入（偏好/事实入库，会话内读个人写），
    # 再召回注入上下文。记忆故障只 warn，绝不阻断调度主流程。
    mem_user = user_id or "anonymous"
    mem_blocks: List[Dict[str, Any]] = []
    try:
        from sca import memory
        await memory.safe_observe_turn(thread_id, mem_user, text)
        mem_blocks = await memory.safe_recall_blocks(thread_id, mem_user, query=text)
    except Exception as exc:  # noqa  记忆子系统整体不可用也不影响调度
        logger.warning("[api] 记忆挂钩不可用（不影响调度）：%s", exc)
    if mem_blocks:
        _session_event(thread_id, "memory",
                       f"注入长期记忆 {len(mem_blocks)} 条", actor="系统",
                       detail={"blocks": [b.get("key") for b in mem_blocks]})

    # F1 仿真档：把注入异常对应的 IoT 读数同步给 gps-mcp（生产环境读数来自真实设备，
    # detect_exception 规则引擎不区分来源，只认遥测数据本身）
    from sca.mcp.client import call as mcp_call
    if anomaly and anomaly.get("code") and (order or {}).get("order_id"):
        try:
            await mcp_call("gps-mcp", "inject_fault_reading",
                           order_id=order["order_id"], code=anomaly["code"])
        except Exception:  # noqa  # 仿真读数失败不阻塞主流程
            pass

    try:
        result = await graph.ainvoke(
            {"order": order, "exception": anomaly, "source_text": text,
             "affected_segment": affected_segment,   # 可选：应急重规划的避让依据
             "memory_blocks": mem_blocks}, config=cfg)
    except GraphInterrupt as exc:  # HITL 中断（垫片路径：抛异常）
        if settings.hitl_enabled:
            _PENDING[thread_id] = graph
            payload = getattr(exc, "payload", None)
            # 挂起前把已算好的方案快照一并回传（前端审批前即可核对解析/路线/运力）
            snap = _scheduling_payload(await _state_values(graph, cfg))
            return {"thread_id": thread_id, "interrupted": True, "level": level,
                    "level_source": level_source, "level_evidence": level_evidence,
                    "order_source": order_source, "order_missing": order_missing,
                    "notes": list(parse_notes or []),
                    "affected_segment": affected_segment or None,
                    "order": snap.get("order") or order,
                    "needs_approval": _needs_approval_from(payload),
                    "interrupt": payload,
                    "session_persisted": _session_hold(thread_id, level, payload),
                    **{k: v for k, v in snap.items() if k != "order"},
                    "message": "已挂起，等待人工审批；POST /api/resume 继续"}
        # HITL 关闭：自动审批通过并继续
        try:
            result = await graph.ainvoke(
                Command(resume={"approved": True, "by": "auto(hitl_disabled)"}), config=cfg)
        except GraphInterrupt:
            _session_finish(thread_id, {}, error="自动审批后仍处挂起（异常）")
            return {"thread_id": thread_id, "interrupted": False,
                    "message": "自动审批后仍处挂起（异常），请检查状态机"}
    except GraphRecursionError as exc:
        # 有界自主：图超步上限耗尽（几乎必然意味着 supervisor 未收敛 / 成环）。
        # 单独识别而非并入泛 Exception —— 否则前端只看到一个 error 字符串，
        # 无法区分「业务降级」与「图根本没跑完」。
        _session_finish(thread_id, {}, error=f"图超步上限耗尽：{exc}")
        return {"thread_id": thread_id, "interrupted": False,
                "error": "graph_recursion_limit", "message": str(exc)}
    except Exception as exc:
        _session_finish(thread_id, {}, error=str(exc))
        return {"thread_id": thread_id, "interrupted": False, "error": str(exc)}
    # 真实 langgraph 路径：中断不抛异常，而是返回带 __interrupt__ 的结果
    if isinstance(result, dict) and result.get("__interrupt__"):
        if settings.hitl_enabled:
            _PENDING[thread_id] = graph
            payload = _interrupt_payload(result)
            # 挂起前把已算好的方案快照一并回传（前端审批前即可核对解析/路线/运力）
            snap = _scheduling_payload(await _state_values(graph, cfg))
            return {"thread_id": thread_id, "interrupted": True, "level": level,
                    "level_source": level_source, "level_evidence": level_evidence,
                    "order_source": order_source, "order_missing": order_missing,
                    "notes": list(parse_notes or []),
                    "affected_segment": affected_segment or None,
                    "order": snap.get("order") or order,
                    "needs_approval": _needs_approval_from(payload),
                    "interrupt": payload,
                    "session_persisted": _session_hold(thread_id, level, payload),
                    **{k: v for k, v in snap.items() if k != "order"},
                    "message": "已挂起，等待人工审批；POST /api/resume 继续"}
        try:
            result = await graph.ainvoke(
                Command(resume={"approved": True, "by": "auto(hitl_disabled)"}), config=cfg)
            if isinstance(result, dict) and result.get("__interrupt__"):
                _session_finish(thread_id, {}, error="自动审批后仍处挂起（异常）")
                return {"thread_id": thread_id, "interrupted": False,
                        "message": "自动审批后仍处挂起（异常），请检查状态机"}
        except Exception as exc:  # noqa
            _session_finish(thread_id, {}, error=str(exc))
            return {"thread_id": thread_id, "interrupted": False, "error": str(exc)}
    out = {"thread_id": thread_id, "interrupted": False, "level": level,
           "level_source": level_source, "level_evidence": level_evidence,
           "order_source": order_source, "order_missing": order_missing,
           "affected_segment": affected_segment or None,
           "notes": list(parse_notes or [])}
    sp = _scheduling_payload(result)
    out.update(sp)
    out["session_persisted"] = _session_finish(thread_id, sp)
    return out


async def h_trigger(payload: Dict[str, Any]) -> Dict:
    """触发一次调度/异常处理。

    等级来源两种（调度员无需人工判定等级）：
      - 自动识别：payload 带 {"text": "..."} 且不带 level（或 level="AUTO"）->
        由故障谱关键词表自动识别故障类型与等级，注入命中的具体异常。
      - 人工覆盖：payload 带 {"level": "P0..P3"} -> 按该等级从故障谱采样。
    返回 level_source ("auto"/"manual") 与 level_evidence（识别依据）。

    高危(P0/P1)在 SCA_HITL_ENABLED=true 时进入 HITL 中断，返回 interrupted=True
    与 thread_id；调用方拿 thread_id 调 /api/resume 完成审批后继续。
    关闭 HITL 时自动审批通过（演示/压测用）。
    """
    try:
        from sca.agents import SupplyChainGraph
        from sca.intent import build_intent_model
        from sca.simulation import FaultInjector
        from sca.simulation.fault_spectrum import infer_from_text, has_anomaly_hint
    except ImportError as exc:
        return {"error": f"依赖未安装：{exc}"}

    text = str(payload.get("text", "") or "").strip()
    manual_level = str(payload.get("level", "") or "").strip().upper()
    # 用户标识（长短期记忆的归属维度）：显式传入；缺省 anonymous（开发默认，
    # 生产必须传入——否则所有匿名会话共享一份记忆）。同 user_id 跨会话延续记忆。
    user_id = str(payload.get("user_id") or "").strip()
    # 会话 ID 在此生成（= LangGraph thread_id），受理被阻断的场景也要留痕
    thread_id = f"api-{uuid.uuid4().hex[:8]}"

    # 订单受理（FR-01）：只认两种来源 —— 调用方显式给的 order，或调度需求文本解析出的运单。
    # 生产环境不存在"默认需求"：信息不足时阻断并提示补录，绝不生成/随机订单代替。
    provided_order = payload.get("order")
    order_source = "provided" if provided_order else ""
    order_missing: List[str] = []
    parsed_order = None
    parse_info: Dict[str, Any] = {}
    parse_notes: List[str] = []
    if text and not provided_order:
        from sca.intake import parse_dispatch_request
        # 同步函数（内置模型兜底会发 HTTP 请求）-> 线程池，避免卡住事件循环
        pr = await run_blocking(parse_dispatch_request, text, use_model=True)
        if pr.get("ok"):
            parse_info = {"source": pr.get("source"), "confidence": pr.get("confidence"),
                          "evidence": pr.get("evidence"), "notes": pr.get("notes") or []}
            order_missing = pr.get("missing") or []
            parse_notes = pr.get("notes") or []
            if pr["order"].get("origin") and pr["order"].get("dest"):
                parsed_order = pr["order"]
                order_source = "parsed"

    # 阻断 1：既无需求文本也无显式 order
    if not provided_order and not text:
        persisted = _session_blocked(thread_id, text, "缺少调度需求文本", ["需求文本"])
        return {"thread_id": thread_id, "session_persisted": persisted,
                "error": "缺少调度需求：请填写需求文本（或显式传入 order），系统不提供默认需求",
                "need_input": True, "missing": ["需求文本"], "order_source": "none"}
    # 阻断 2：有文本但解析不出起讫地 —— 无法落单，要求补录（不回退到随机订单）
    if not provided_order and not parsed_order:
        reason = "需求信息不足：未能识别起运地/目的地，无法受理为运单"
        persisted = _session_blocked(thread_id, text, reason,
                                     [m for m in order_missing if m in ("origin", "dest")])
        return {"thread_id": thread_id, "session_persisted": persisted, "error": reason,
                "need_input": True, "missing": [m for m in order_missing if m in ("origin", "dest")],
                "order_missing": order_missing, "parsed": parse_info,
                "message": "请在描述中写明起运地与目的地（如：从上海发往北京）后重新提交"}

    level_source, level_evidence, anomaly = "manual", None, None
    level = manual_level or "P2"
    if text and (not manual_level or manual_level == "AUTO"):
        # 自动识别：文本 -> 故障类型/等级（FR-05 分级自动化）
        inf = infer_from_text(text)
        if inf["matched"]:
            level_source = "auto"
            level_evidence = {"keyword": inf["keyword"], "fault": inf["name"],
                              "code": inf["code"]}
            anomaly = FaultInjector().inject_code(inf["code"])
            level = anomaly.get("level") or level
        else:
            # 未命中关键词：**不随机编造故障**。原实现在此处 FaultInjector().inject("P2")
            # 随机抽一个 P2 异常，导致"货物淋雨"被显示成"施工绕行"。现在再分两种：
            #   ① 描述里有异常线索（如"路上出了点状况"）-> E-UNKNOWN，P2，提示人工确认；
            #   ② 完全没有异常线索（纯下单需求）-> 无异常，按常规调度 P3 处理，
            #      既不编造故障，也不硬套一个 P2 异常处置流程。
            hint = has_anomaly_hint(text)
            level_source = "auto"
            if hint:
                level = "P2"
                level_evidence = {"keyword": None, "fault": None, "code": None,
                                  "hint": hint,
                                  "note": f"描述含异常线索「{hint}」，但未匹配故障谱，"
                                          f"按默认 P2 记录并提示人工确认"}
                anomaly = {"code": "E-UNKNOWN", "name": "未识别异常", "level": "P2",
                           "desc": f"描述含异常线索「{hint}」，系统未能匹配到具体故障类型，"
                                   f"按默认 P2 记录并提示人工确认",
                           "subsystems": [], "auto": False, "source": "unclassified",
                           "ts": int(time.time())}
            else:
                level = "P3"
                level_evidence = {"keyword": None, "fault": None, "code": None,
                                  "note": "需求未包含任何异常/故障描述，按常规调度处理"
                                          "（不进入异常处置链，不做应急绕行重规划）"}
                anomaly = None
    if anomaly is None and level_source == "manual":
        # 仅当人工显式指定等级（压测/演示）时才按等级采样；文本路径已给出确定性结果
        anomaly = FaultInjector().inject(level)

    # 收货方手机号（供钉钉群内 @ 到收货方本人）：显式传入优先，其次用需求文本解析结果。
    # 属于真实个人信息，不做任何生成/补全；格式不合法直接阻断并提示。
    from sca.mcp.notify_server import valid_phone as _valid_phone
    recv_phone = str(payload.get("receiver_phone") or payload.get("contact") or "").strip()
    if recv_phone and not _valid_phone(recv_phone):
        persisted = _session_blocked(thread_id, text, "收货方手机号格式不正确", ["receiver_phone"])
        return {"thread_id": thread_id, "session_persisted": persisted,
                "error": "收货方手机号格式不正确（应为 11 位中国大陆手机号）",
                "need_input": True, "missing": ["receiver_phone"]}

    # 订单号优先级：调用方显式传入的 order.order_id > 异常描述区录入的订单号 >
    # 需求文本里解析到的单号 > 系统生成（后两者已在 intake 组装 order 时落定）
    order = dict(parsed_order or provided_order or {})
    ext_order_id = _read_order_id(payload)
    if ext_order_id and not str((provided_order or {}).get("order_id") or "").strip():
        order["order_id"] = ext_order_id

    # 订单与异常确定后，交给与「历史会话重新处理」共用的执行流程（两条路径必须同源，
    # 否则一边加了挂起处理、另一边会漏）
    return await _dispatch_flow(
        thread_id=thread_id, text=text, order=order,
        anomaly=anomaly, level_source=level_source, level_evidence=level_evidence,
        order_source=order_source, order_missing=order_missing,
        receiver_phone=recv_phone, user_id=user_id, level=level,
        parse_notes=parse_notes, affected_segment=_read_segment(payload))


async def h_resume(payload: Dict[str, Any]) -> Dict:
    """恢复 HITL 挂起线程：decision={"approved": true} 等。

    两级恢复：
      1) 进程内 _PENDING（内存检查点 / 同进程恢复）；
      2) PG 检查点（跨进程/重启恢复）：按 thread_id 从 PG 重建现场并续跑。
    """
    thread_id = payload.get("thread_id")
    graph = _PENDING.get(thread_id)
    if graph is None:
        # PG 检查点兜底：跨进程/重启后，_PENDING 丢失但 PG 里有现场
        try:
            from sca.agents import SupplyChainGraph
            from sca.intent import build_intent_model
            from sca.storage import pg_checkpointer

            cp = pg_checkpointer()
            if cp is not None:
                g = SupplyChainGraph(intent_model=build_intent_model())
                graph = g.compile(checkpointer=cp)
        except Exception as exc:  # noqa
            return {"error": f"thread {thread_id} 恢复失败：{exc}"}
    if graph is None:
        return {"error": f"thread {thread_id} 不存在或已过期"}
    decision = payload.get("decision") or {"approved": True}
    # recursion_limit 必须显式设置：官方默认 10007（≈无上限），垫片读同一环境变量。
    # 引入 supervisor 循环后，"没有上限"就等于"死循环风险"。
    cfg = {"configurable": {"thread_id": thread_id},
           "recursion_limit": settings.recursion_limit}
    approved = bool(decision.get("approved")) if isinstance(decision, dict) else True
    _session_event(thread_id, "resume",
                   f"调度员决策：{'通过' if approved else '驳回'}",
                   actor="调度员",
                   detail={"decision": decision if isinstance(decision, dict) else None})
    # 挂起未解除时必须保留现场：一次处置可能连续两次中断（高危审批 -> 调度结果审核），
    # 若在第一次恢复后就 pop，第二次审核将因找不到图而失败。只有真正跑完才清理。
    try:
        result = await graph.ainvoke(Command(resume=decision), config=cfg)
    except GraphInterrupt as exc:
        _PENDING[thread_id] = graph
        payload = getattr(exc, "payload", None)
        return {"thread_id": thread_id, "interrupted": True,
                "needs_approval": _needs_approval_from(payload),
                "interrupt": payload,
                "session_persisted": _session_hold(thread_id, "", payload),
                "message": "仍存在未决审批，继续 POST /api/resume"}
    except GraphRecursionError as exc:
        _PENDING.pop(thread_id, None)
        _session_finish(thread_id, {}, error=f"图超步上限耗尽：{exc}")
        return {"thread_id": thread_id, "error": "graph_recursion_limit",
                "message": str(exc)}
    except Exception as exc:
        _PENDING.pop(thread_id, None)
        _session_finish(thread_id, {}, error=str(exc))
        return {"thread_id": thread_id, "error": str(exc)}
    # 真实 langgraph 路径：中断以 __interrupt__ 键返回
    if isinstance(result, dict) and result.get("__interrupt__"):
        _PENDING[thread_id] = graph
        payload = _interrupt_payload(result)
        return {"thread_id": thread_id, "interrupted": True,
                "needs_approval": _needs_approval_from(payload),
                "interrupt": payload,
                "session_persisted": _session_hold(thread_id, "", payload),
                "message": "仍存在未决审批，继续 POST /api/resume"}
    _PENDING.pop(thread_id, None)
    out = {"thread_id": thread_id, "interrupted": False}
    sp = _scheduling_payload(result)
    out.update(sp)
    out["session_persisted"] = _session_finish(thread_id, sp)
    return out


async def _purge_thread_checkpoints(thread_id: str) -> bool:
    """删除该 thread 的旧检查点，让「重新处理」是一次全新运行。

    为什么必须删：LangGraph 在同一 thread 上再次 ainvoke，会把旧 checkpoint 的
    通道值（notifications / approval / dispatch_review / trace…）合并进新 run——
    notifications 会在旧列表上追加，造成**重复推送**；审批/审核结论也会串味。
    「重新处理」语义上就该是干净的一次运行，旧现场只应留在会话时间线里可回放。
    PG 不可用（MemorySaver 每次编译都是新实例）时本就无跨 run 旧状态，无需删除。
    """
    try:
        from sca.storage import pg_checkpointer
        cp = pg_checkpointer()
        if cp is None:
            return True   # 内存检查点：无跨 run 旧状态可清
        try:
            await cp.adelete_thread(thread_id)
        finally:
            await cp.aclose()   # 一次性使用者：归还连接，避免池子泄漏
        return True
    except Exception as exc:  # noqa: 清失败不阻断重跑（记日志 + 响应里标记）
        logger.warning("[api] 清除线程 %s 旧检查点失败（继续重跑）：%s", thread_id, exc)
        return False


# 原地重处理防重入：同一会话同时只允许一次重新处理（图执行是异步的）
_REPROC_INFLIGHT: set = set()


async def h_session_continue(payload: Dict[str, Any]) -> Dict:
    """从历史会话继续处理：**在同一会话上原地重新处理**（session_id / thread_id 不变）。

    解决的痛点：调度员把会话驳回/阻断/跑挂后，此前无法再从历史入口把它重新驱动
    到底（只能回控制台手敲一条新需求）。现在点「继续处理」即把这个会话原地重跑。

    与「新开一条会话」方案的区别（那是已废弃的旧行为）：
      * **会话 ID 不变**：续处理就是把这一个会话重新驱动到底，历史时间线在原地
        追加，不分裂成父子两条；审批/审核也在同一 thread 上继续，进度不锁定；
      * **可反复重处理**：支持调度员先补录/修订需求文本（如 blocked 会话补上
        起运地/目的地）与收货方手机号再跑——修订后重新解析，解析不出起讫地则
        明确提示且不动会话状态。旧行为用原文重跑，blocked 会话只会再 blocked，
        陷入「重开→再阻断→再重开」的无限新会话死循环；
      * **不写 continued_by / parent_session_id**：旧会话进度不再被"续接锁定"。

    状态机约束：
      * awaiting_approval / awaiting_review：默认仍走控制台 /api/resume 继续；
        **带 rewind=true 时允许回退**——放弃当前挂起现场，回退到输入步骤
        （调度员在两种人工闸口被驳回/想改需求时用），同会话原地重跑；
      * done：已完结，重跑会重复向司机/收货方下发通知，拒绝；
      * rejected / error / blocked：可原地重处理。
    """
    sid = str((payload or {}).get("session_id") or "").strip()
    if not sid:
        return {"error": "缺少 session_id"}
    s = _sessions()
    if not s:
        return {"error": "会话历史模块不可用"}
    rec = s.get_session(sid)
    if not rec:
        return {"error": f"未找到会话 {sid}"}
    status = rec.get("status")
    rewind = bool((payload or {}).get("rewind"))
    # 仍处挂起（awaiting_approval / awaiting_review）的会话默认走控制台 /resume 继续，
    # 不要从历史入口重开，否则会生成重复的 live 线程。
    # 例外：rewind=true（「回退到输入重填」）——调度员在人工闸口被驳回或想改需求时，
    # 主动放弃挂起现场回退到输入步骤：_reprocess_session 会弹掉 _PENDING 里的旧图、
    # 清掉该 thread 旧检查点后同 ID 原地重跑（审批闸口在 execute 之前，无已执行副作用；
    # 审核闸口在 notify 之前，通知尚未发出，重跑不会重复下发）。
    if status in ("awaiting_approval", "awaiting_review"):
        if not rewind:
            return {"error": "该会话仍处挂起，请直接用控制台审批/审核继续（/api/resume），无需重新处理",
                    "status": status, "still_live": True,
                    "hint": "如需放弃当前挂起、回退到输入步骤修改需求，请使用「回退到输入重填」"}
    elif status == "done":
        return {"error": "会话已完结（done），无需重新处理；重跑会重复向司机/收货方下发通知",
                "status": status}
    elif status not in ("rejected", "error", "blocked"):
        return {"error": f"会话状态为 {status}，非「已驳回 / 异常终止 / 受理阻断」，无法重新处理",
                "status": status}
    if sid in _REPROC_INFLIGHT:
        return {"error": "该会话正在重新处理中，请稍候", "status": status}

    _REPROC_INFLIGHT.add(sid)
    try:
        return await _reprocess_session(s, sid, rec, payload or {})
    finally:
        _REPROC_INFLIGHT.discard(sid)


async def _reprocess_session(s, sid: str, rec: Dict[str, Any],
                             payload: Dict[str, Any]) -> Dict:
    """原地重处理的公共路径：解析输入 -> 重置会话 -> 同一 thread 重跑状态机。"""
    from sca.intake import parse_dispatch_request
    from sca.simulation import FaultInjector
    from sca.simulation.fault_spectrum import infer_from_text, has_anomaly_hint

    inp = rec.get("input") or {}
    old_text = inp.get("text") or ""
    # 1) 需求文本：调度员可修订（补录起讫地等）；未提供则复用原文
    provided_text = str(payload.get("text") or "").strip()
    text = provided_text or old_text
    # 2) 收货方手机号（可选）。运单快照里只有脱敏值、不可反向还原，
    #    需要 @ 司机/收货方时由调度员本次重新填写；格式不合法直接拒绝，不动会话状态。
    from sca.mcp.notify_server import valid_phone as _valid_phone
    recv_phone = str(payload.get("receiver_phone") or payload.get("contact") or "").strip()
    if recv_phone and not _valid_phone(recv_phone):
        return {"error": "收货方手机号格式不正确（应为 11 位中国大陆手机号）",
                "need_input": True, "missing": ["receiver_phone"],
                "session_unchanged": True}
    manual_level = str(payload.get("level") or "").strip().upper()
    # 受影响路段（可选）：本次显式传入优先，否则复用会话里上次录入的值
    # （「补录后重新处理 / 回退重填」不应要求调度员重复录入同一路段）
    affected_segment = (_read_segment(payload)
                        or str(inp.get("affected_segment") or "").strip())

    # 3) 运单来源：
    #    a. 调度员修订了文本 -> 重新解析（补录信息的正常入口）；解析不出起讫地则
    #       阻断提示，会话状态保持不变（不消耗任何东西）
    #    b. 未修订文本 -> 复用会话里的运单快照；无快照（老会话/受理即阻断）时按
    #       input 受理信息重建，尽量复用原运单身份而不是把原文解析成一张新单
    order_source, order_missing, parse_info = "from_history", [], {}
    parse_notes: List[str] = []
    if provided_text:
        pr = await run_blocking(parse_dispatch_request, text, use_model=True)
        parse_info = {"source": pr.get("source"), "confidence": pr.get("confidence"),
                      "evidence": pr.get("evidence"), "notes": pr.get("notes") or []}
        order_missing = pr.get("missing") or []
        parse_notes = pr.get("notes") or []
        if not (pr.get("ok") and (pr.get("order") or {}).get("origin")
                and (pr.get("order") or {}).get("dest")):
            return {"error": "修订后的需求信息仍不足：未能识别起运地/目的地，无法受理为运单",
                    "need_input": True,
                    "missing": [m for m in order_missing if m in ("origin", "dest")],
                    "order_missing": order_missing, "parsed": parse_info,
                    "message": "请在描述中写明起运地与目的地（如：从上海发往北京）后重新提交",
                    "session_unchanged": True}
        order = pr["order"]
        order_source = "revised"
    else:
        order = dict(rec.get("order") or {})
        if not order and (inp.get("order_id") or inp.get("origin")):
            # 老会话（运单快照功能上线前创建）或阻断类会话没有顶层 order 快照时，
            # 用 input 里的受理信息重建运单（temp_ctrl/hazmat 由状态机按 cargo.type 推导）
            order = {"order_id": inp.get("order_id"), "origin": inp.get("origin"),
                     "dest": inp.get("dest"), "cargo": inp.get("cargo") or {},
                     "source_text": text}
        if not order:
            return {"error": "该会话没有可复用的运单信息（受理即被阻断），"
                             "请通过「补录信息后重新处理」修改需求，或在控制台重新提交",
                    "need_input": True, "missing": ["需求文本"],
                    "session_unchanged": True}

    # 3b) 订单号（关键信息）：本次显式录入优先于会话快照里的原单号——调度员补录/纠正
    #     单号后应按新单号绑定承运商（未填则沿用原单号，不因重处理而丢身份）。
    ext_order_id = _read_order_id(payload)
    if ext_order_id:
        order = {**order, "order_id": ext_order_id}

    # 4) 异常与等级：人工指定 > 修订文本重新识别 > 原异常码规则重建。
    #    未命中故障谱时不随机编造：有异常线索 -> E-UNKNOWN P2 提示人工；
    #    完全无异常线索 -> 无异常，常规调度 P3（与 /trigger 同口径，见 has_anomaly_hint）。
    level = manual_level or "P2"
    if manual_level and manual_level != "AUTO":
        anomaly = FaultInjector().inject(manual_level)
        level_source, level_evidence = "manual", None
        level = anomaly.get("level") or level
    elif provided_text:
        inf = infer_from_text(text)
        level_source = "auto"
        if inf["matched"]:
            level_evidence = {"keyword": inf["keyword"], "fault": inf["name"],
                              "code": inf["code"]}
            anomaly = FaultInjector().inject_code(inf["code"])
            level = anomaly.get("level") or level
        else:
            hint = has_anomaly_hint(text)
            if hint:
                level = "P2"
                level_evidence = {"keyword": None, "fault": None, "code": None,
                                  "hint": hint,
                                  "note": f"描述含异常线索「{hint}」，但未匹配故障谱，"
                                          f"按默认 P2 记录并提示人工确认"}
                anomaly = {"code": "E-UNKNOWN", "name": "未识别异常", "level": "P2",
                           "desc": f"描述含异常线索「{hint}」，系统未能匹配到具体故障类型，"
                                   f"按默认 P2 记录并提示人工确认",
                           "subsystems": [], "auto": False, "source": "unclassified",
                           "ts": int(time.time())}
            else:
                level = "P3"
                level_evidence = {"keyword": None, "fault": None, "code": None,
                                  "note": "需求未包含任何异常/故障描述，按常规调度处理"
                                          "（不进入异常处置链，不做应急绕行重规划）"}
                anomaly = None
    else:
        exc_code = (rec.get("exception") or {}).get("code")
        anomaly = _anomaly_from_code(exc_code)
        level_source = rec.get("level_source") or "auto"
        level_evidence = rec.get("level_evidence")
        level = rec.get("level") or (anomaly or {}).get("level") or level

    # 5) 重置会话 + 清理旧执行现场，准备同一 thread 的全新运行
    prev_status = rec.get("status") or ""
    prev_label = s.STATUS_LABEL.get(prev_status, prev_status)
    s.reprocess(sid, prev_status=prev_label, revised=bool(provided_text),
                rewind=bool(payload.get("rewind")))
    _PENDING.pop(sid, None)   # 旧挂起现场（若有）失效
    purged = await _purge_thread_checkpoints(sid)
    logger.info("[api] 会话 %s（原状态=%s，修订=%s）原地重新处理，thread 不变，检查点已清=%s",
                sid, prev_status, bool(provided_text), purged)

    # 6) 同一 thread 重跑：session_id 即 thread_id，HITL 挂起/收尾与 /trigger 同路径
    sp = await _dispatch_flow(
        thread_id=sid, text=text, order=order, anomaly=anomaly,
        level_source=level_source, level_evidence=level_evidence,
        order_source=order_source, order_missing=order_missing,
        receiver_phone=recv_phone, user_id=str(payload.get("user_id") or ""),
        level=level, parse_notes=parse_notes, affected_segment=affected_segment)
    sp["session_id"] = sid
    sp["reprocessed"] = True
    sp["checkpoint_purged"] = purged
    if provided_text:
        sp["input_revised"] = True
    return sp


# ---------------------------------------------------------------------------
# 重新审批：把 thread 回退到 interrupt 检查点（与「重新处理」整场重跑互补）
# ---------------------------------------------------------------------------
_INTERRUPT_CHANNEL = "__interrupt__"


async def _rewind_to_interrupt(thread_id: str, phase: str) -> Dict[str, Any]:
    """把 LangGraph thread 回退到当初的 interrupt 检查点，让 /resume 能从原中断点续跑。

    为什么需要：调度员在「调度结果审核」闸口驳回后，状态机已经跑到 END（检查点链
    一路记到 end 节点），直接 Command(resume=...) 找不到挂起任务。回退 = 删掉中断点
    之后的检查点链，让 interrupt 检查点重新成为最新现场。

    必须删干净的四样（少删任何一样，重新审批都会拿到旧结论或旧数据）：
      1) target 检查点上**除 __interrupt__ 之外**的 pending writes——里面混着上次
         被中断任务已产出的 dispatch_review/trace/notifications/branch 写行，以及
         已消费的 __resume__ 应答，不清掉会被当成"待应用的写"合并进新 run；
      2) target 之后的全部 checkpoint_writes（已跑完的后续节点的产出）；
      3) target 之后的全部 checkpoints；
      4) 被删检查点**引入**、而保留检查点不再引用的 (channel, version) 对应的
         checkpoint_blobs——langgraph 按 (thread, channel, version) 寻址 blob 且
         插入是 ON CONFLICT DO NOTHING，回退后新 run 会复用同一 version 号，旧 blob
         （如上一次 dispatch_review=rejected）会被静默沿用，终态与详情页全串味。

    返回 {"ok": True, "checkpoint_id", "interrupt"}；找不到中断现场时 ok=False
    （调用方应提示改用「重新处理」）。PG 不可用时同样 ok=False（无现场可恢复）。
    """
    from sca.storage import pg_checkpointer
    cp = pg_checkpointer()
    if cp is None:
        return {"ok": False, "reason": "no_pg"}
    try:
        # 1) 找中断点：最新的、interrupt 类型与驳回点一致的检查点
        #    （review = notify 里的调度审核中断；approval = approval_gate 高危审批中断）
        target, interrupt_payload = None, None
        async for tup in cp.alist({"configurable": {"thread_id": thread_id}}):
            for (task_id, ch, val) in (tup.pending_writes or []):
                if ch != _INTERRUPT_CHANNEL:
                    continue
                iv = getattr(val[0], "value", val[0]) if isinstance(val, list) and val else val
                iv = iv if isinstance(iv, dict) else {}
                if (phase == "review") != (iv.get("type") == "dispatch_review"):
                    continue
                target, interrupt_payload = tup.checkpoint["id"], iv
                break
            if target:
                break
        if not target:
            return {"ok": False, "reason": "interrupt_not_found"}

        # 2) 过期 blob：被删检查点引入、保留检查点不再引用的 (channel, version)
        deleted_vers: set = set()
        keep_vers: set = set()
        async for tup in cp.alist({"configurable": {"thread_id": thread_id}}):
            vers = {k: str(v) for k, v in
                    (tup.checkpoint.get("channel_versions") or {}).items() if v is not None}
            bucket = deleted_vers if tup.checkpoint["id"] > target else keep_vers
            bucket.update((k, v) for k, v in vers.items())
        for ch, ver in sorted(deleted_vers - keep_vers):
            await cp.aexecute(
                "DELETE FROM checkpoint_blobs WHERE thread_id=%s AND checkpoint_ns=%s "
                "AND channel=%s AND version=%s",
                (thread_id, "", ch, ver))
        # 3) target 上除 __interrupt__ 外的写行（旧任务产出 + 已消费的 resume 应答）
        await cp.aexecute(
            "DELETE FROM checkpoint_writes WHERE thread_id=%s AND checkpoint_id=%s "
            "AND channel <> %s",
            (thread_id, target, _INTERRUPT_CHANNEL))
        # 4) target 之后的写行与检查点
        await cp.aexecute(
            "DELETE FROM checkpoint_writes WHERE thread_id=%s AND checkpoint_id > %s",
            (thread_id, target))
        await cp.aexecute(
            "DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_id > %s",
            (thread_id, target))
        return {"ok": True, "checkpoint_id": target, "interrupt": interrupt_payload}
    except Exception as exc:  # noqa: 回退失败不炸会话，由调用方降级提示
        logger.warning("[api] 回退线程 %s 检查点失败：%s", thread_id, exc)
        return {"ok": False, "reason": str(exc)}
    finally:
        await cp.aclose()


async def h_session_reapprove(payload: Dict[str, Any]) -> Dict:
    """重新审批：把已驳回（rejected）的会话打回当初的挂起点，调度员在控制台重新审批/审核。

    解决的痛点：调度员在「调度结果审核」闸口驳回后，会话进入 rejected 终态，此前只能
    「重新处理」——而重新处理是整场重跑（重新规划路径、重新匹配运力、重新识别异常）。
    很多时候调度员只是**改个结论**（驳回错了 / 想改了），不该把整条链路重跑一遍。
    重新审批把 LangGraph thread 回退到当初的 interrupt 检查点：调度员重新点「通过」，
    就从原中断点继续（下发通知 -> 复盘 -> 落库），会话 ID / thread_id 全程不变，
    也不产生新会话、不重复规划。

    与「重新处理」的分工：
      * 重新审批：不重跑状态机，只把结论重新给一遍——适合"驳回错了，想改结论"；
      * 重新处理：整场重跑（可修订需求文本/手机号）——适合"需求本身要改"。

    状态机约束：仅 rejected 可重新审批；awaiting_* 请走控制台 /resume；
    done 无需重新审批；blocked / error 请改用「重新处理」。
    """
    sid = str((payload or {}).get("session_id") or "").strip()
    if not sid:
        return {"error": "缺少 session_id"}
    s = _sessions()
    if not s:
        return {"error": "会话历史模块不可用"}
    rec = s.get_session(sid)
    if not rec:
        return {"error": f"未找到会话 {sid}"}
    status = rec.get("status")
    if status in ("awaiting_approval", "awaiting_review"):
        return {"error": "该会话仍处挂起，请直接用控制台审批/审核继续（/api/resume），无需重新审批",
                "status": status, "still_live": True}
    if status == "done":
        return {"error": "会话已完结（done），无需重新审批", "status": status}
    if status != "rejected":
        return {"error": f"会话状态为 {status}，非「已驳回」，无法重新审批；"
                         "受理阻断 / 异常终止的会话请改用「重新处理」",
                "status": status, "fallback": "reprocess"}
    # 驳回点：调度结果审核（notify 中断点）或高危审批（approval_gate 中断点）
    iv = rec.get("interrupt") or {}
    dr = rec.get("dispatch_review") or {}
    phase = "review" if (isinstance(iv, dict) and iv.get("type") == "dispatch_review") \
        or (isinstance(dr, dict) and dr.get("status") == "rejected") else "approval"

    rw = await _rewind_to_interrupt(sid, phase)
    if not rw.get("ok"):
        if rw.get("reason") == "no_pg":
            return {"error": "当前未启用 PG 检查点，无法恢复中断现场；请改用「重新处理」",
                    "status": status, "fallback": "reprocess"}
        return {"error": "该会话的中断现场已丢失（可能已被「重新处理」清掉），请改用「重新处理」",
                "status": status, "fallback": "reprocess"}

    _PENDING.pop(sid, None)   # 旧挂起现场（若有）失效，/resume 时从 PG 重建
    s.reapprove(sid, prev_status=s.STATUS_LABEL.get(status, status), phase=phase)
    interrupt_payload = rw.get("interrupt") or (iv if isinstance(iv, dict) else {})
    if interrupt_payload:
        s.patch(sid, interrupt=interrupt_payload)
    logger.info("[api] 会话 %s 重新审批：回退到 %s 中断点（检查点 %s），thread 不变",
                sid, phase, (rw.get("checkpoint_id") or "")[:8])
    new_status = s.STATUS_REVIEW if phase == "review" else s.STATUS_APPROVAL
    return {"thread_id": sid, "session_id": sid, "reapproved": True,
            "phase": phase, "status": new_status,
            "interrupted": True, "interrupt": interrupt_payload,
            "checkpoint_restored": (rw.get("checkpoint_id") or "")[:8],
            "message": f"会话已打回{'待审核下发' if phase == 'review' else '待审批'}，请在控制台重新审批"}


# ---------------------------------------------------------------------------
# 知识库（FR-10）
# ---------------------------------------------------------------------------
async def h_knowledge_list() -> Dict:
    from sca.knowledge_base import list_sops, summary
    return {"summary": summary(), "sops": list_sops()}


async def h_knowledge_search(payload: Dict[str, Any]) -> Dict:
    from sca.knowledge_base import search_sop
    query = str(payload.get("query", "")).strip()
    code = str(payload.get("code", "")).strip()
    top_k = int(payload.get("top_k", 5) or 5)
    return {"query": query, "results": search_sop(query, top_k=top_k, code=code)}


async def h_knowledge_upsert(payload: Dict[str, Any]) -> Dict:
    """新增/更新 SOP（知识库更新）。同名 sop_id 覆盖内置，可恢复内置。"""
    from sca.knowledge_base import upsert_sop, summary
    res = upsert_sop(payload or {})
    if res.get("ok"):
        res["summary"] = summary()
    return res


async def h_knowledge_delete(payload: Dict[str, Any]) -> Dict:
    """删除自定义 SOP / 撤销对内置 SOP 的覆盖。"""
    from sca.knowledge_base import delete_sop, summary
    res = delete_sop(str((payload or {}).get("sop_id", "")).strip())
    if res.get("ok"):
        res["summary"] = summary()
    return res


async def h_knowledge_rebuild() -> Dict:
    """重建知识库：内置 SOP 由故障谱重新生成 + 检索索引重建（自定义条目保留）。"""
    from sca.knowledge_base import rebuild
    return rebuild()


# ---------------------------------------------------------------------------
# 系统设置（在线模型 / 流程开关），持久化到 .env
# ---------------------------------------------------------------------------
async def h_config_get() -> Dict:
    from sca.config_io import masked_config
    return masked_config()


async def h_config_update(payload: Dict[str, Any]) -> Dict:
    from sca.config_io import update_config, masked_config
    # 仅当提供了非空新 Key 才覆盖（避免前端回传打码值把真 Key 冲掉）
    mapping = dict(payload)
    if "dashscope_api_key" in mapping:
        k = str(mapping.get("dashscope_api_key") or "").strip()
        if not k or "…" in k or k == "已配置":
            mapping.pop("dashscope_api_key")
    applied = update_config(mapping)
    return {"applied": applied, "config": masked_config(),
            "message": "配置已保存并即时生效（意图模型随下次调用重建）"}


async def h_config_test(payload: Dict[str, Any]) -> Dict:
    """测试在线模型连通性（最小调用，1 token）。优先用表单临时值，否则用当前配置。"""
    key = str(payload.get("dashscope_api_key") or "").strip() or settings.dashscope_api_key
    base = str(payload.get("dashscope_base_url") or "").strip() or settings.dashscope_base_url
    model = str(payload.get("intent_model") or "").strip() or settings.intent_model
    if not key:
        return {"ok": False, "detail": "未配置 API Key"}
    if "…" in key or key == "已配置":
        key = settings.dashscope_api_key  # 打码值 -> 用真实 Key

    import time
    import urllib.error
    import urllib.request

    def _call():
        url = base.rstrip("/") + "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=12) as r:
            json.loads(r.read().decode("utf-8"))
        return round((time.time() - t0) * 1000)

    try:
        ms = await asyncio.to_thread(_call)
        return {"ok": True, "latency_ms": ms, "model": model, "detail": "连接成功"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "model": model, "detail": f"HTTP {e.code}（鉴权/模型名有误？）"}
    except Exception as e:  # noqa
        return {"ok": False, "model": model, "detail": str(e)}


async def h_route(payload: Dict[str, Any]) -> Dict:
    """自然语言 -> 意图识别 + 工具路由（FR-12，原型期核心价值入口）。"""
    try:
        from sca.intent import build_intent_model
        from sca.agents import TOOL_REGISTRY
    except ImportError as exc:
        return {"error": f"依赖未安装：{exc}"}
    text = payload.get("text", "")
    model = build_intent_model()
    res = await model.classify(text, TOOL_REGISTRY, {})
    # 同步给出等级/故障预判（与 /trigger 的自动识别同一套规则，口径一致）
    from sca.simulation.fault_spectrum import infer_from_text
    inf = infer_from_text(text)
    return {"text": text, "intent": res.intent, "tool_calls": res.tool_calls,
            "confidence": res.confidence, "source": res.source,
            "fault_inference": inf}


# ---------------------------------------------------------------------------
# 前端静态目录解析
# ---------------------------------------------------------------------------
def resolve_frontend_dir() -> str:
    if settings.frontend_dir:
        return settings.frontend_dir
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "frontend")


# ---------------------------------------------------------------------------
# FastAPI 装配（create_app 工厂）
# ---------------------------------------------------------------------------
def create_app() -> "FastAPI":
    """构建 FastAPI 应用：/api 前缀 + CORS + 挂载前端静态。"""
    if not HAVE_FASTAPI:
        raise RuntimeError("未安装 fastapi/uvicorn，无法构建 ASGI 应用；"
                           "请 pip install -r requirements.txt，或用 python -m sca.api 零依赖回退")

    prefix = settings.api_prefix.rstrip("/")
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

    app = FastAPI(title="供应链多智能体调度系统", version="0.1.0")

    # CORS：允许前端独立托管（前后端分离开发）
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get(prefix + "/health")
    async def _health():
        return await h_health()

    @app.get(prefix + "/eval")
    async def _eval():
        return JSONResponse(await h_eval())

    @app.get(prefix + "/dashboard-data")
    async def _dashboard_data():
        return JSONResponse(await h_dashboard_data())

    @app.post(prefix + "/trigger")
    async def _trigger(payload: Dict[str, Any]):
        return await h_trigger(payload)

    @app.post(prefix + "/resume")
    async def _resume(payload: Dict[str, Any]):
        return await h_resume(payload)

    @app.post(prefix + "/route")
    async def _route(payload: Dict[str, Any]):
        return await h_route(payload)

    @app.post(prefix + "/parse")
    async def _parse(payload: Dict[str, Any]):
        return await h_parse(payload)

    @app.get(prefix + "/knowledge")
    async def _knowledge():
        return JSONResponse(await h_knowledge_list())

    @app.post(prefix + "/knowledge/search")
    async def _knowledge_search(payload: Dict[str, Any]):
        return await h_knowledge_search(payload)

    @app.post(prefix + "/knowledge/upsert")
    async def _knowledge_upsert(payload: Dict[str, Any]):
        return await h_knowledge_upsert(payload)

    @app.post(prefix + "/knowledge/delete")
    async def _knowledge_delete(payload: Dict[str, Any]):
        return await h_knowledge_delete(payload)

    @app.post(prefix + "/knowledge/rebuild")
    async def _knowledge_rebuild():
        return await h_knowledge_rebuild()

    @app.get(prefix + "/config")
    async def _config_get():
        return await h_config_get()

    @app.post(prefix + "/config/update")
    async def _config_update(payload: Dict[str, Any]):
        return await h_config_update(payload)

    @app.post(prefix + "/config/test")
    async def _config_test(payload: Dict[str, Any]):
        return await h_config_test(payload)

    @app.get(prefix + "/keys/check")
    async def _keys_check(which: str = "all"):
        return JSONResponse(await h_keys_check(which))

    @app.get(prefix + "/credentials")
    async def _credentials_list():
        return JSONResponse(await h_credentials_list())

    @app.post(prefix + "/credentials/upsert")
    async def _credentials_upsert(payload: Dict[str, Any]):
        return JSONResponse(await h_credentials_upsert(payload))

    @app.post(prefix + "/credentials/delete")
    async def _credentials_delete(payload: Dict[str, Any]):
        return JSONResponse(await h_credentials_delete(payload))

    @app.post(prefix + "/credentials/test")
    async def _credentials_test(payload: Dict[str, Any]):
        return JSONResponse(await h_credentials_test(payload))

    @app.post(prefix + "/credentials/reset")
    async def _credentials_reset():
        return JSONResponse(await h_credentials_reset())

    @app.get(prefix + "/sessions/stats")
    async def _sessions_stats():
        return JSONResponse(await h_sessions_stats())

    @app.get(prefix + "/sessions")
    async def _sessions_list(status: str = "", level: str = "", q: str = "",
                             limit: int = 50, offset: int = 0):
        return JSONResponse(await h_sessions_list(
            {"status": status, "level": level, "q": q, "limit": limit, "offset": offset}))

    @app.get(prefix + "/sessions/{sid}")
    async def _sessions_detail(sid: str):
        return JSONResponse(await h_sessions_detail(sid))

    @app.post(prefix + "/sessions/delete")
    async def _sessions_delete(payload: Dict[str, Any]):
        return JSONResponse(await h_sessions_delete(payload))

    @app.post(prefix + "/sessions/clear")
    async def _sessions_clear(payload: Dict[str, Any]):
        return JSONResponse(await h_sessions_clear(payload))

    @app.post(prefix + "/sessions/continue")
    async def _sessions_continue(payload: Dict[str, Any]):
        return JSONResponse(await h_session_continue(payload))

    @app.post(prefix + "/sessions/reapprove")
    async def _sessions_reapprove(payload: Dict[str, Any]):
        return JSONResponse(await h_session_reapprove(payload))

    @app.get(prefix + "/carriers")
    async def _carriers_list():
        return await h_carriers_list()

    @app.post(prefix + "/carriers/upsert")
    async def _carriers_upsert(payload: Dict[str, Any]):
        return await h_carriers_upsert(payload)

    @app.post(prefix + "/carriers/delete")
    async def _carriers_delete(payload: Dict[str, Any]):
        return await h_carriers_delete(payload)

    # 挂载前端静态站点（前后端分离：后端只出 JSON，前端是独立静态资源）
    fe_dir = resolve_frontend_dir()
    if os.path.isdir(fe_dir):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=fe_dir, html=True), name="frontend")
        logger.info("[api] 前端静态目录已挂载: %s", fe_dir)
    else:
        logger.warning("[api] 前端目录不存在，跳过静态挂载: %s", fe_dir)

    return app


async def h_keys_check(which: str = "all") -> Dict:
    """高德 / 和风 Key 真实连通性自检（未配置时返回获取步骤，不发网络请求）。"""
    try:
        from sca.keys_check import check_all
        # check_all 会逐个发真实请求（高德/和风）；同步调用会阻塞事件循环
        return await run_blocking(check_all, (which or "all").lower())
    except Exception as exc:
        return {"items": [], "all_ok": False, "any_missing": True,
                "error": str(exc)}


async def h_credentials_list() -> Dict:
    """凭证清单（模型 + 工具），密钥脱敏，含健康状态。"""
    try:
        from sca.credentials import summary
        return summary()
    except Exception as exc:
        return {"models": [], "tools": [], "error": str(exc)}


async def h_credentials_upsert(payload: Dict[str, Any]) -> Dict:
    kind = str((payload or {}).get("kind") or "tool").strip()
    if kind not in ("model", "tool"):
        return {"ok": False, "error": "kind 须为 model 或 tool"}
    from sca.credentials import upsert_credential
    return upsert_credential(kind, payload)


async def h_credentials_delete(payload: Dict[str, Any]) -> Dict:
    kind = str((payload or {}).get("kind") or "tool").strip()
    cid = str((payload or {}).get("id") or "").strip()
    if not cid:
        return {"ok": False, "error": "id 必填"}
    from sca.credentials import delete_credential
    return delete_credential(kind, cid)


async def h_credentials_reset() -> Dict:
    """清空所有凭证的失败计数（熔断复位）。"""
    from sca.credentials import reset_health
    return reset_health()


async def h_credentials_test(payload: Dict[str, Any]) -> Dict:
    """真实测试某条凭证是否可用（未保存的临时值也可测）。

    不编造：真实调用官方接口；钉钉为真实发送（会往群里推一条自检消息）。
    """
    kind = str((payload or {}).get("kind") or "tool").strip()
    cid = str((payload or {}).get("id") or "").strip()
    from sca.credentials import get_credential, mark_fail, mark_ok
    cred = get_credential(kind, cid) if cid else None
    if cred is None:  # 未保存：用提交上来的临时值
        cred = {"id": "tmp", "name": (payload or {}).get("name") or "临时",
                "service": (payload or {}).get("service") or "llm",
                "data": (payload or {}).get("data") or payload}
    d = cred.get("data") or {}
    try:
        if kind == "model":
            from sca.intent import TongyiIntentDetectV3
            m = TongyiIntentDetectV3(
                (d.get("api_key") or "").strip(),
                (d.get("base_url") or "").strip()
                or "https://dashscope.aliyuncs.com/compatible-mode/v1",
                (d.get("intent_model") or "").strip() or "tongyi-intent-detect-v3",
                timeout=float(d.get("timeout") or 5.0))
            r = await m.try_classify("帮我查一下运单状态", [])
            ok = r is not None
            detail = (f"模型连通（{d.get('intent_model') or '默认'}，"
                      f"意图={r.intent if r else '未解析'}）" if ok
                      else "模型无响应或鉴权失败")
        else:
            service = (cred.get("service") or "").strip()
            if service == "amap":
                import urllib.parse
                from sca.keys_check import _open
                key = (d.get("key") or "").strip()
                if not key:
                    return {"ok": False, "detail": "未填 Key"}
                q = urllib.parse.urlencode({"key": key, "origin": "121.4737,31.2304",
                                            "destination": "120.1551,30.2741",
                                            "extensions": "base"})
                j = await run_blocking(_open,
                                       f"https://restapi.amap.com/v3/direction/driving?{q}")
                if j.get("status") != "1":
                    return {"ok": False, "detail": f"高德返回 {j.get('info')}（{j.get('infocode')}）"}
                p = ((j.get("route") or {}).get("paths") or [{}])[0]
                w = await run_blocking(_open, "https://restapi.amap.com/v3/weather/weatherInfo?"
                                       + urllib.parse.urlencode({"key": key, "city": "北京",
                                                                 "extensions": "all"}))
                cast = ((w.get("forecasts") or [{}])[0].get("casts") or [{}])[0]
                ok = True
                detail = (f"路径 {float(p.get('distance', 0)) / 1000:.1f}km；"
                          f"天气 北京 {cast.get('dayweather') or '?'} "
                          f"{cast.get('daytemp') or '?'}℃")
            elif service == "qweather":
                from sca.mcp import weather_server as ws
                key, host = (d.get("key") or "").strip(), (d.get("host") or "").strip()
                if not key:
                    return {"ok": False, "detail": "未填 Key"}
                # 这三个都是同步网络函数（内部 _http_get 走 urlopen）-> 线程池
                h = await run_blocking(ws._detect_host, key, host)
                lid = await run_blocking(ws._lookup_city, "北京", key, h) if h else None
                if not lid:
                    return {"ok": False, "detail": "城市查询失败（Key 无效或城市不支持）"}
                j = await run_blocking(ws._http_get, f"{h}/v7/weather/now",
                                       {"location": lid, "key": key})
                now = j.get("now") or {}
                ok = True
                detail = f"和风连通（{h}）：北京 {now.get('text')} {now.get('temp')}℃"
            elif service == "dingtalk":
                from sca.mcp import notify_server as ns
                wh, sec = (d.get("webhook") or "").strip(), (d.get("secret") or "").strip()
                if not (wh and sec):
                    return {"ok": False, "detail": "webhook 与 secret 均必填"}
                r = await ns.send_dispatch_notice(
                    target="凭证自检",
                    text="### 凭证清单 · 连通性自检\n- 该消息由「工具 Key 清单」测试按钮真实发出",
                    title="供应链调度系统 · 凭证自检")
                ok = bool(r.get("sent"))
                detail = ("已真实推送钉钉群（" + (r.get("channel") or "") + "）" if ok
                          else f"推送失败：{r.get('error') or r.get('reason')}")
            else:
                return {"ok": False, "detail": f"未知服务 {service or '空'}"}
        if cid:
            if ok:
                mark_ok(kind, cid)
            else:
                mark_fail(kind, cid, detail)
        return {"ok": bool(ok), "detail": detail, "id": cid or None}
    except Exception as exc:
        if cid:
            mark_fail(kind, cid, str(exc))
        return {"ok": False, "detail": str(exc)[:300]}


# 模块级 app，兼容 `uvicorn sca.api:app`
app = create_app() if HAVE_FASTAPI else None


# ---------------------------------------------------------------------------
# 标准库回退服务（无 fastapi 时，同样 /api 前缀 + 托管前端）
# ---------------------------------------------------------------------------
def _stdlib_server(host: str = "0.0.0.0", port: int = 8000):
    import http.server
    import urllib.parse
    from http import HTTPStatus

    prefix = settings.api_prefix.rstrip("/")
    fe_dir = resolve_frontend_dir()

    ROUTES = {
        prefix + "/health": ("GET", h_health, "json"),
        prefix + "/eval": ("GET", h_eval, "json"),
        prefix + "/dashboard-data": ("GET", h_dashboard_data, "json"),
        prefix + "/trigger": ("POST", h_trigger, "json"),
        prefix + "/resume": ("POST", h_resume, "json"),
        prefix + "/route": ("POST", h_route, "json"),
        prefix + "/parse": ("POST", h_parse, "json"),
        prefix + "/knowledge": ("GET", h_knowledge_list, "json"),
        prefix + "/knowledge/search": ("POST", h_knowledge_search, "json"),
        prefix + "/knowledge/upsert": ("POST", h_knowledge_upsert, "json"),
        prefix + "/knowledge/delete": ("POST", h_knowledge_delete, "json"),
        prefix + "/knowledge/rebuild": ("POST", h_knowledge_rebuild, "json"),
        prefix + "/config": ("GET", h_config_get, "json"),
        prefix + "/config/update": ("POST", h_config_update, "json"),
        prefix + "/config/test": ("POST", h_config_test, "json"),
        prefix + "/keys/check": ("GET", h_keys_check, "json"),
        prefix + "/credentials": ("GET", h_credentials_list, "json"),
        prefix + "/credentials/upsert": ("POST", h_credentials_upsert, "json"),
        prefix + "/credentials/delete": ("POST", h_credentials_delete, "json"),
        prefix + "/credentials/test": ("POST", h_credentials_test, "json"),
        prefix + "/credentials/reset": ("POST", h_credentials_reset, "json"),
        prefix + "/carriers": ("GET", h_carriers_list, "json"),
        prefix + "/carriers/upsert": ("POST", h_carriers_upsert, "json"),
        prefix + "/carriers/delete": ("POST", h_carriers_delete, "json"),
        prefix + "/sessions/delete": ("POST", h_sessions_delete, "json"),
        prefix + "/sessions/clear": ("POST", h_sessions_clear, "json"),
        prefix + "/sessions/continue": ("POST", h_session_continue, "json"),
        prefix + "/sessions/reapprove": ("POST", h_session_reapprove, "json"),
    }

    _CT = "application/json; charset=utf-8"

    def _serve_static(handler, path: str) -> bool:
        if not fe_dir or not os.path.isdir(fe_dir):
            return False
        rel = urllib.parse.unquote(path).lstrip("/")
        rel = rel.replace("..", "")  # 防目录穿越
        fpath = os.path.join(fe_dir, rel or "index.html")
        if not os.path.isfile(fpath):
            return False
        ext = os.path.splitext(fpath)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(fpath, "rb") as fh:
            body = fh.read()
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", _CT)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            # 动态路由：/api/sessions[/{id}|/stats]（列表/详情/统计走查询串与路径后缀）
            if method == "GET" and path.startswith(prefix + "/sessions"):
                try:
                    if path == prefix + "/sessions/stats":
                        res = asyncio.run(h_sessions_stats())
                    elif path == prefix + "/sessions":
                        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                        res = asyncio.run(h_sessions_list(qs))
                    else:
                        sid = urllib.parse.unquote(path[len(prefix + "/sessions/"):]).strip("/")
                        res = asyncio.run(h_sessions_detail(sid))
                    self._send_json(HTTPStatus.OK, res)
                except Exception as exc:  # noqa
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            route = ROUTES.get(path)
            if route is not None and route[0] == method:
                _, fn, _ = route
                try:
                    if method == "POST":
                        length = int(self.headers.get("Content-Length", 0) or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        payload = json.loads(raw or b"{}")
                        coro = fn(payload)
                    else:
                        coro = fn()
                    result = asyncio.run(coro) if asyncio.iscoroutine(coro) else coro
                    self._send_json(HTTPStatus.OK, result)
                except Exception as exc:  # noqa
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            # 非 API 路径 -> 尝试静态文件
            if method == "GET" and _serve_static(self, path):
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": path})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_OPTIONS(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def log_message(self, *args):  # 静默默认访问日志
            pass

    srv = http.server.HTTPServer((host, port), Handler)
    logger.warning("[api] 未安装 fastapi，使用标准库回退服务 http://%s:%d", host, port)
    print(f"供应链多智能体系统 API（标准库回退）已启动: http://localhost:{port}")
    print(f"路由前缀: {prefix}   前端目录: {fe_dir or '(未配置)'}")
    print("示例: curl -X POST http://localhost:8000" + prefix + "/route -H 'Content-Type: application/json' -d '{\"text\":\"冷链车温度超标\"}'")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if HAVE_FASTAPI and app is not None:
        import uvicorn
        uvicorn.run(app, host=settings.host, port=settings.port,
                    log_level=settings.log_level)
    else:
        _stdlib_server(host=settings.host, port=settings.port)
