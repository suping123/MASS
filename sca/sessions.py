"""会话历史（Session History）：把每一次调度/异常处置会话完整落盘，可检索、可回放。

为什么要有这一层
----------------
系统此前只有两类痕迹：进程内 ``_PENDING``（重启即失）与 ``data/notifications.jsonl``
（只有通知回执）。**处置过程本身没有可追溯记录**——谁在什么时间提了什么需求、
识别成什么异常、走了哪条意图链路、用了哪些 MCP 工具、审批/审核是谁做的、最后发出去没有，
全都查不到。会话历史就是补这一层：以 LangGraph 的 ``thread_id`` 作为会话唯一标识，
把一次处置的**多轮交互**（触发 → HITL 审批 → 调度员审核 → 下发 → 结束）串成一条时间线。

设计要点
--------
1. **会话 = thread**：``session_id`` 直接复用 ``thread_id``，端到端对齐，便于从日志反查。
2. **落盘与容量**：``data/sessions.json`` 单文件 + 原子写（tmp → ``os.replace``）；
   超过 ``MAX_SESSIONS`` 条时把最旧的一批归档到 ``data/sessions_archive_<ts>.json``，
   避免单文件无限膨胀。
3. **脱敏（硬约束）**：手机号一律 ``138****8000``；需求原文里的 11 位手机号同样打码；
   **任何 API Key / Secret / Webhook 都不进入会话记录**（只记模型名与凭证名）。
   执行方式：唯一写盘路口 ``_write_doc`` 对整档做递归深脱敏（``_mask_strings``）——
   不按 key 名穷举（``source_text`` 等自由文本夹带号码时穷举会漏），新增字段自动生效。
4. **失败不静默**：写盘失败要记日志并返回 False；调用方（api.py）据此在响应里标出
   ``session_persisted=false``，不让"没存上"伪装成"存上了"。
5. **有限等待**：进程内锁带超时（5s），避免异常路径下把请求线程永久挂住。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sca.sessions")

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "sessions.json"

MAX_SESSIONS = 500        # 热数据保留条数，超出部分归档
ARCHIVE_BATCH = 200       # 每次归档的条数
LOCK_TIMEOUT = 5.0        # 进程内锁等待上限（秒）
WRITE_RETRIES = 3         # 原子写重试次数
TRACE_KEEP = 60           # 每条会话最多保留的 trace 行数
MCP_KEEP = 40             # 每条会话最多保留的 MCP 调用证据条数
TEXT_KEEP = 500           # 需求原文最多保留字符数

_LOCK = threading.RLock()

# 状态机：与 api.py / agents 的挂起语义一一对应
STATUS_RUNNING = "running"
STATUS_APPROVAL = "awaiting_approval"   # P0/P1 高危，等人工审批
STATUS_REVIEW = "awaiting_review"       # 调度结果待调度员审核后下发
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"            # 审批拒绝或调度员驳回
STATUS_ERROR = "error"
STATUS_BLOCKED = "blocked"              # 受理阶段信息不足被阻断

STATUS_LABEL = {
    STATUS_RUNNING: "进行中",
    STATUS_APPROVAL: "待审批",
    STATUS_REVIEW: "待审核下发",
    STATUS_DONE: "已完成",
    STATUS_REJECTED: "已驳回",
    STATUS_ERROR: "异常终止",
    STATUS_BLOCKED: "受理阻断",
}

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


# ---------------------------------------------------------------------------
# 脱敏（手机号）：会话记录里任何位置都不存明文号码
# ---------------------------------------------------------------------------
def mask_phone(v: Any) -> Optional[str]:
    """138****8000；非法或空返回 None（不编造）。"""
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace("-", "")
    return (s[:3] + "****" + s[-4:]) if _PHONE_RE.match(s) else None


def mask_text(text: str) -> str:
    """需求原文脱敏：把正文里出现的手机号打码，其余内容保留（回放需要）。"""
    if not text:
        return ""
    return _PHONE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], str(text))


def _safe(v: Any, keep: int = 200) -> Any:
    """字段级截断，避免异常大的 payload 撑爆单文件。"""
    if isinstance(v, str):
        return v[:keep]
    return v


# ---------------------------------------------------------------------------
# 落盘（原子写 + 重试 + 归档）
# ---------------------------------------------------------------------------
def _default_doc() -> Dict[str, Any]:
    return {"version": 1, "updated_ts": time.time(), "sessions": []}


def _load_doc() -> Dict[str, Any]:
    """读取；文件损坏则把坏文件改名保留（不丢证据）后返回空档。"""
    if _PATH.is_file():
        try:
            doc = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("sessions"), list):
                doc.setdefault("version", 1)
                doc.setdefault("updated_ts", time.time())
                return doc
        except Exception as exc:
            logger.warning("[sessions] 读取失败，备份坏文件后重建：%s", exc)
            try:
                _PATH.replace(_PATH.with_suffix(f".broken-{int(time.time())}.json"))
            except Exception:  # noqa: 备份失败不影响主流程
                pass
    return _default_doc()


def _mask_strings(v: Any) -> Any:
    """递归深脱敏：任何层级、任何字段的字符串值里出现手机号一律打码（硬约束）。

    为什么不能只按 key 名排除（contact/receiver_phone）：运单快照的
    ``source_text``、执行轨迹等自由文本字段都可能夹带明文号码——实测就
    漏过（parse 出的 source_text 原样进了快照）。穷举 key 名挡不住未来
    新增字段，因此在唯一写盘路口统一深脱敏，保证"任何号码不入档"。
    已脱敏值（138****8000）不含 11 位连续数字，重复脱敏是幂等的。
    """
    if isinstance(v, str):
        return mask_text(v)
    if isinstance(v, dict):
        return {k: _mask_strings(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_mask_strings(x) for x in v]
    return v


def _write_doc(doc: Dict[str, Any]) -> bool:
    """原子写（tmp → os.replace），失败重试 3 次（指数退避 + 抖动）。

    写盘前对整档做一次手机号深脱敏：脱敏是硬约束，就在唯一写盘路口强制
    执行，不依赖每个调用方各自记得——包括未来新增的字段。
    """
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_mask_strings(doc), ensure_ascii=False)
    last: Optional[Exception] = None
    for i in range(WRITE_RETRIES):
        tmp = _PATH.with_name(f"{_PATH.name}.tmp-{os.getpid()}-{i}")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, _PATH)
            return True
        except Exception as exc:
            last = exc
            try:
                if tmp.is_file():
                    tmp.unlink()
            except Exception:  # noqa: 清理失败忽略
                pass
            time.sleep(0.05 * (2 ** i) + random.uniform(0, 0.02))
    logger.error("[sessions] 写入失败（已重试 %d 次）：%s", WRITE_RETRIES, last)
    return False


def _archive(doc: Dict[str, Any]) -> None:
    """热数据超限时，把最旧的一批移到归档文件（保留证据，不删除）。"""
    sessions = doc.get("sessions", [])
    if len(sessions) <= MAX_SESSIONS:
        return
    sessions.sort(key=lambda s: s.get("created_ts") or 0)
    moved, doc["sessions"] = sessions[:ARCHIVE_BATCH], sessions[ARCHIVE_BATCH:]
    dst = _PATH.parent / f"sessions_archive_{int(time.time())}.json"
    try:
        dst.write_text(json.dumps({"version": 1, "archived_ts": time.time(),
                                   "sessions": _mask_strings(moved)}, ensure_ascii=False),
                       encoding="utf-8")
        logger.info("[sessions] 已归档 %d 条到 %s", len(moved), dst.name)
    except Exception as exc:  # 归档失败不阻断：热数据仍在
        logger.warning("[sessions] 归档失败：%s", exc)


def _locked(fn):
    """带超时的锁包装：拿不到锁直接抛错，绝不无限等待。"""
    def wrapper(*args, **kwargs):
        if not _LOCK.acquire(timeout=LOCK_TIMEOUT):
            raise RuntimeError(f"sessions 锁等待超过 {LOCK_TIMEOUT}s，放弃本次写入")
        try:
            return fn(*args, **kwargs)
        finally:
            _LOCK.release()
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", "")
    return wrapper


def _find(doc: Dict[str, Any], sid: str) -> Optional[Dict[str, Any]]:
    for s in doc.get("sessions", []):
        if s.get("session_id") == sid:
            return s
    return None


# ---------------------------------------------------------------------------
# 会话生命周期
# ---------------------------------------------------------------------------
@_locked
def start(session_id: str, *, source: str = "api", text: str = "",
          order: Optional[Dict[str, Any]] = None, level: str = "",
          level_source: str = "", level_evidence: Optional[Dict[str, Any]] = None,
          exception: Optional[Dict[str, Any]] = None,
          receiver_phone: str = "",
          affected_segment: str = "") -> Dict[str, Any]:
    """开启一条会话记录。重复 session_id 视为同会话，不覆盖已有事件。"""
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        rec = {
            "session_id": session_id,
            "source": source,
            "created_ts": time.time(),
            "updated_ts": time.time(),
            "status": STATUS_RUNNING,
            "events": [],
        }
        doc["sessions"].append(rec)
    o = order or {}
    rec["input"] = {
        "text": mask_text(text)[:TEXT_KEEP],
        "text_len": len(text or ""),
        "order_id": o.get("order_id"),
        "origin": o.get("origin"),
        "dest": o.get("dest"),
        "cargo": _safe(o.get("cargo"), 120),
        "receiver_phone_masked": mask_phone(receiver_phone or o.get("contact")),
        # 可选录入的受影响路段（应急重规划的避让依据）：供详情页复核与
        # 「重新处理 / 回退重填」时带回，避免调度员重复录入
        "affected_segment": (affected_segment or "")[:120],
    }
    # 运单快照：供"从历史会话继续处理"时复用（contact 是真实手机号，只存脱敏值，不入快照）
    if o:
        rec["order"] = {k: v for k, v in o.items() if k not in ("contact", "receiver_phone")}
    rec["level"] = level
    rec["level_source"] = level_source
    rec["level_evidence"] = level_evidence or None
    if exception:
        rec["exception"] = {"code": exception.get("code"), "name": exception.get("name"),
                            "level": exception.get("level")}
    rec["updated_ts"] = time.time()
    _archive(doc)
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return rec


# 原地「重新处理」时要清空的终态/产出字段（事件时间线保留，审计可追溯）。
# 为什么必须清：同一会话再跑一次时，上一运行的终态（status/duration）、调度产出
# （summary/notifications/dispatch_review/trace…）若残留，详情页会新旧数据混杂，
# 调度员会以为通知已经发过。events / created_ts / parent_session_id 不动。
_RESET_ON_REPROCESS = ("ended_ts", "duration_ms", "error", "summary",
                       "notifications", "notify_total", "notify_sent",
                       "dispatch_review", "intent", "trace", "mcp_calls",
                       "sop", "continued_by", "interrupt")


@_locked
def reprocess(session_id: str, *, prev_status: str = "",
              revised: bool = False, rewind: bool = False) -> Optional[Dict[str, Any]]:
    """原地重新处理：重置终态与产出字段，状态回到 running，时间线追加事件。

    与 start() 的分工：start() 只更新受理信息（input/order/level/exception），
    面向「第一次受理」；reprocess() 面向「同一会话再跑一次」，必须把上一次
    运行的终态与产出清空，否则详情页新旧数据混杂、通知回执张冠李戴。
    事件时间线（events）只追加不覆盖：重处理次数、上次为什么失败都可回放。

    rewind=True：本次是从挂起态（高危审批/调度审核被驳回或想改需求）
    「回退到输入步骤」——放弃挂起现场后原地重跑，事件标签据此区分。
    """
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        return None
    for k in _RESET_ON_REPROCESS:
        rec.pop(k, None)
    rec["status"] = STATUS_RUNNING
    rec["updated_ts"] = time.time()
    label = ("回退到输入步骤（原状态：%s）" if rewind else "重新处理（原状态：%s）") \
        % (prev_status or "未知")
    ev: Dict[str, Any] = {"ts": time.time(), "type": "reprocess",
                          "actor": "调度员", "label": label}
    if revised:
        ev["detail"] = {"input_revised": True}
    rec.setdefault("events", []).append(ev)
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return rec


# 「重新审批」打回挂起时要清空的字段：终态 + 上一次通知相关产出。
# 为什么不学 reprocess 清 intent/trace/mcp_calls/sop：重新审批是从原中断点继续，
# 那些字段仍是本次运行的有效产出（不会重新识别/检索），清了详情页反而丢信息。
# interrupt 不清——它会被调用方用回退到的中断点载荷刷新。
_RESET_ON_REAPPROVE = ("ended_ts", "duration_ms", "error", "summary",
                       "notifications", "notify_total", "notify_sent",
                       "dispatch_review")


@_locked
def reapprove(session_id: str, *, prev_status: str = "",
              phase: str = "review") -> Optional[Dict[str, Any]]:
    """重新审批：把已驳回（rejected）的会话打回当初的挂起态，等调度员重新审批/审核。

    与 reprocess() 的分工：reprocess 是"整场重跑"（从 entry 重新走状态机）；
    reapprove 是"回到当初的挂起点"——LangGraph thread 已由调用方回退到 interrupt
    检查点，这里只把会话状态与通知相关产出复位，事件时间线只追加不覆盖。
    """
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        return None
    for k in _RESET_ON_REAPPROVE:
        rec.pop(k, None)
    is_review = phase != "approval"
    rec["status"] = STATUS_REVIEW if is_review else STATUS_APPROVAL
    rec["updated_ts"] = time.time()
    rec.setdefault("events", []).append({
        "ts": time.time(), "type": "reapprove", "actor": "调度员",
        "label": f"重新审批：打回{'待审核下发' if is_review else '待审批'}"
                 f"（原状态：{prev_status or '未知'}）",
        "detail": {"phase": "review" if is_review else "approval"},
    })
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return rec


@_locked
def event(session_id: str, etype: str, label: str, actor: str = "system",
          detail: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """追加一条时间线事件（触发 / 挂起 / 审批 / 审核 / 通知 / 结束）。"""
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        logger.warning("[sessions] 会话 %s 不存在，事件未记录：%s", session_id, etype)
        return None
    ev = {"ts": time.time(), "type": etype, "actor": actor, "label": label}
    if detail:
        ev["detail"] = _safe_detail(detail)
    rec.setdefault("events", []).append(ev)
    rec["updated_ts"] = time.time()
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return ev


def _safe_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    """事件详情脱敏：手机号打码，超大值截断。"""
    out: Dict[str, Any] = {}
    for k, v in (detail or {}).items():
        if isinstance(v, str) and _PHONE_RE.search(v):
            v = mask_text(v)
        if k in ("phone", "driver_phone", "contact", "receiver_phone"):
            v = mask_phone(v) or v
        out[k] = _safe(v, 300)
    return out


@_locked
def patch(session_id: str, **fields) -> Optional[Dict[str, Any]]:
    """更新会话字段（status / interrupt / 中间态），不做深合并之外的动作。"""
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        return None
    for k, v in fields.items():
        if v is None:
            continue
        rec[k] = v
    rec["updated_ts"] = time.time()
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return rec


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    """从调度产出报文里抽出「一眼能看懂」的摘要（不含密钥）。"""
    route = result.get("route") or {}
    cap = (result.get("capacity") or [{}])
    sop = result.get("sop") or {}
    cost = result.get("cost") or {}
    eta = result.get("eta") or {}
    eta_h = eta.get("est_h") if isinstance(eta, dict) else None
    return {
        "plan_version": result.get("plan_version"),
        "sop_id": sop.get("id") or sop.get("sop_id"),
        "sop_name": sop.get("name") or sop.get("title"),
        "carrier": (cap[0].get("name") if isinstance(cap[0], dict) else None),
        "route_action": (result.get("route_decision") or {}).get("action"),
        "distance_km": route.get("distance_km"),
        "route_provider": route.get("provider"),
        "eta_h": eta_h,
        "eta_min": round(float(eta_h) * 60, 1) if eta_h else None,
        "cost": (cost.get("est") if isinstance(cost, dict) else None),
        "currency": (cost.get("currency") if isinstance(cost, dict) else None),
        "replan_ms": (result.get("metrics") or {}).get("replan_ms"),
        "within_budget": result.get("within_budget"),
    }


def _safe_review(dr: Dict[str, Any]) -> Dict[str, Any]:
    """审核快照脱敏：只留状态/标题/摘要/正文与"是否 @"，手机号一律打码。

    存它的用途只有一个：调度员从历史会话「继续处理」时，能看到上次待审核的方案是什么。
    因此不需要（也不允许）留存任何明文号码。
    """
    out: Dict[str, Any] = {
        "status": dr.get("status"),
        "reason": _safe(dr.get("reason"), 200),
        "by": _safe(dr.get("by"), 60),
        "title": _safe(dr.get("title"), 160),
        "summary": _safe(dr.get("summary"), 240),
    }
    if dr.get("content"):
        out["content"] = _safe(mask_text(str(dr["content"])), 1500)
    # 调度员侧内部信息（SOP 编号 / 路线决策 / 未改道原因 / 模型裁决 / 实测耗时）：
    # 只随审核快照给调度员回看，不随对外通知下发。
    if dr.get("detail"):
        out["detail"] = _safe(mask_text(str(dr["detail"])), 800)
    tg: List[Dict[str, Any]] = []
    for t in (dr.get("targets") or []):
        if not isinstance(t, dict):
            continue
        tg.append({"role": t.get("role"), "at": bool(t.get("at")),
                   "phone_masked": t.get("phone_masked") or mask_phone(t.get("phone"))})
    out["targets"] = tg
    return out


def _notifications(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for n in (result.get("notifications") or []):
        if not isinstance(n, dict):
            continue
        out.append({
            "target": n.get("target"),
            "channel": n.get("channel"),
            "sent": bool(n.get("sent")),
            "at_mobiles_masked": n.get("at_mobiles_masked") or [],
            "reason": n.get("reason") or n.get("error"),
        })
    return out


@_locked
def finish(session_id: str, status: str, *, result: Optional[Dict[str, Any]] = None,
           error: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """结束会话：写入最终状态、摘要、通知回执、工具证据与耗时。"""
    doc = _load_doc()
    rec = _find(doc, session_id)
    if rec is None:
        return None
    now = time.time()
    rec["status"] = status
    rec["ended_ts"] = now
    rec["updated_ts"] = now
    rec["duration_ms"] = int((now - float(rec.get("created_ts") or now)) * 1000)
    if error:
        rec["error"] = str(error)[:300]
    if result:
        intent = result.get("intent") or {}
        rec["intent"] = {
            "intent": intent.get("intent"),
            "source": intent.get("source"),
            "model_name": intent.get("model_name"),
            "confidence": intent.get("confidence"),
            "attempts": intent.get("attempts") or [],
        }
        rec["summary"] = _summarize(result)
        rec["notifications"] = _notifications(result)
        rec["notify_total"] = len(rec["notifications"])
        rec["notify_sent"] = sum(1 for n in rec["notifications"] if n.get("sent"))
        mcp = [m for m in (result.get("mcp_calls") or []) if isinstance(m, dict)]
        rec["mcp_calls"] = [{"server": m.get("server"), "tool": m.get("tool"),
                             "ok": m.get("ok")} for m in mcp][:MCP_KEEP]
        trace = result.get("trace") or []
        rec["trace"] = [str(t) for t in trace][-TRACE_KEEP:]
        dr = result.get("dispatch_review") or {}
        if dr:
            rec["dispatch_review"] = _safe_review(dr)
        ap = result.get("sop") or {}
        if ap:
            rec["sop"] = {"id": ap.get("id") or ap.get("sop_id"),
                          "name": ap.get("name") or ap.get("title")}
        if result.get("exception"):
            e = result["exception"]
            rec["exception"] = {"code": e.get("code"), "name": e.get("name"),
                                "level": e.get("level")}
    ok = _write_doc(doc)
    rec["_persisted"] = ok
    return rec


# ---------------------------------------------------------------------------
# 查询 / 统计 / 删除
# ---------------------------------------------------------------------------
def _brief(rec: Dict[str, Any]) -> Dict[str, Any]:
    """列表用摘要：不带事件与 trace，控制响应体积。"""
    inp = rec.get("input") or {}
    exc = rec.get("exception") or {}
    return {
        "session_id": rec.get("session_id"),
        "created_ts": rec.get("created_ts"),
        "updated_ts": rec.get("updated_ts"),
        "duration_ms": rec.get("duration_ms"),
        "status": rec.get("status"),
        "status_label": STATUS_LABEL.get(rec.get("status"), rec.get("status")),
        "resumable": rec.get("status") in (STATUS_APPROVAL, STATUS_REVIEW),
        "parent_session_id": rec.get("parent_session_id"),
        "continued_by": rec.get("continued_by"),
        "level": rec.get("level") or exc.get("level"),
        "exception": exc,
        "source": rec.get("source"),
        "origin": inp.get("origin"),
        "dest": inp.get("dest"),
        "order_id": inp.get("order_id"),
        "excerpt": (inp.get("text") or "")[:80],
        "notify_total": rec.get("notify_total", 0),
        "notify_sent": rec.get("notify_sent", 0),
        "event_count": len(rec.get("events") or []),
        "carrier": (rec.get("summary") or {}).get("carrier"),
        "error": rec.get("error"),
    }


def list_sessions(status: str = "", level: str = "", q: str = "",
                  limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """按状态/等级/关键词检索（关键词匹配原文、异常名、城市、会话 ID、运单号）。"""
    with _LOCK:
        doc = _load_doc()
        items = list(doc.get("sessions", []))
    items.sort(key=lambda s: s.get("created_ts") or 0, reverse=True)
    st, lv, kw = (status or "").strip(), (level or "").strip(), (q or "").strip().lower()
    if st:
        items = [s for s in items if s.get("status") == st]
    if lv:
        items = [s for s in items
                 if (s.get("level") or (s.get("exception") or {}).get("level")) == lv]
    if kw:
        def hit(s: Dict[str, Any]) -> bool:
            inp = s.get("input") or {}
            hay = " ".join(str(x or "") for x in (
                inp.get("text"), inp.get("origin"), inp.get("dest"), inp.get("order_id"),
                (s.get("exception") or {}).get("name"), (s.get("exception") or {}).get("code"),
                s.get("session_id"), (s.get("summary") or {}).get("carrier"))).lower()
            return kw in hay
        items = [s for s in items if hit(s)]
    total = len(items)
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    page = items[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset,
            "sessions": [_brief(s) for s in page]}


def get_session(sid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        doc = _load_doc()
        rec = _find(doc, sid)
        return json.loads(json.dumps(rec, ensure_ascii=False)) if rec else None


def stats() -> Dict[str, Any]:
    with _LOCK:
        doc = _load_doc()
        items = list(doc.get("sessions", []))
    by_status: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    durs: List[float] = []
    notify_total = notify_sent = 0
    for s in items:
        by_status[s.get("status", "?")] = by_status.get(s.get("status", "?"), 0) + 1
        lv = s.get("level") or (s.get("exception") or {}).get("level") or "?"
        by_level[lv] = by_level.get(lv, 0) + 1
        if s.get("duration_ms"):
            durs.append(float(s["duration_ms"]))
        notify_total += int(s.get("notify_total") or 0)
        notify_sent += int(s.get("notify_sent") or 0)
    durs.sort()
    p95 = durs[int(len(durs) * 0.95) - 1] if len(durs) >= 20 else (durs[-1] if durs else 0)
    return {
        "total": len(items),
        "by_status": by_status,
        "by_level": by_level,
        "avg_duration_ms": int(sum(durs) / len(durs)) if durs else 0,
        "p95_duration_ms": int(p95),
        "notify_total": notify_total,
        "notify_sent": notify_sent,
        "notify_success_rate": round(notify_sent / notify_total, 4) if notify_total else None,
        "last_ts": max((s.get("created_ts") or 0) for s in items) if items else None,
        "max_sessions": MAX_SESSIONS,
    }


@_locked
def delete_session(sid: str) -> Dict[str, Any]:
    doc = _load_doc()
    before = len(doc["sessions"])
    doc["sessions"] = [s for s in doc["sessions"] if s.get("session_id") != sid]
    if len(doc["sessions"]) == before:
        return {"ok": False, "error": "未找到该会话"}
    ok = _write_doc(doc)
    return {"ok": ok, "id": sid}


@_locked
def clear_sessions(confirm: str) -> Dict[str, Any]:
    """清空会话历史：需要显式确认串，防误触（C6 危险操作闸口）。"""
    if (confirm or "").strip() != "CLEAR":
        return {"ok": False, "error": "需传 confirm=CLEAR 才会执行清空"}
    doc = _load_doc()
    n = len(doc["sessions"])
    try:
        dst = _PATH.parent / f"sessions_archive_{int(time.time())}.json"
        dst.write_text(json.dumps({"version": 1, "archived_ts": time.time(),
                                   "sessions": doc["sessions"]}, ensure_ascii=False),
                       encoding="utf-8")
    except Exception as exc:  # 归档失败 -> 拒绝清空，避免证据丢失
        return {"ok": False, "error": f"归档失败，已取消清空：{exc}"}
    doc["sessions"] = []
    ok = _write_doc(doc)
    return {"ok": ok, "cleared": n}
