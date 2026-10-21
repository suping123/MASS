"""异常分级与处置：把承运商上报的一段话，变成"几级、影响多大、多久内要处置、走哪条 SOP"。

这里**不重新发明故障谱**：异常码、等级、SOP 映射全部复用 `sca.simulation.fault_spectrum`
（与调度内核同一份）。平台层只做两件事：
  1. 结合订单字段把影响范围说清楚（货类、吨位、时效要求）；
  2. 补上处置时限与是否需要人工审批——这是调度员真正需要的决策输入。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from sca.simulation.fault_spectrum import by_code, has_anomaly_hint, infer_from_text, sop_for

from ..models import ExceptionReport, ShipperOrder
from . import audit
from .orders import STATUS_LABEL, add_event, transition

# 分级策略（对应需求 FR-05 / 第 10 节）：
#   P0 立即人工 + 自动应急；P1 自动重规划 + 通知 + 人工确认；
#   P2 自动换路/调 ETA；P3 记录 + 建议。
LEVEL_POLICY: Dict[str, Dict[str, Any]] = {
    "P0": {"sla_min": 30, "approval": True,
           "action": "立即人工介入 + 自动应急：先保人保货，再定责",
           "auto": "通知调度主管与货主，同步启动应急 SOP"},
    "P1": {"sla_min": 120, "approval": True,
           "action": "自动重规划（改道/延时），通知相关方并人工确认",
           "auto": "生成备选路线与新的 ETA，等待调度确认后下发"},
    "P2": {"sla_min": 240, "approval": False,
           "action": "自动调整路线或 ETA，不需要人工审批",
           "auto": "更新 ETA 并记录事件"},
    "P3": {"sla_min": 480, "approval": False,
           "action": "记录并给出建议，不影响主流程",
           "auto": "写入事件流，标记待复盘"},
}

# 这些货类出问题时的实际损失远高于普通货，等级要往上抬一档
_ESCALATE_CARGO = ("医药冷链", "生鲜冷链", "危险品")
_LEVEL_ORDER = ["P0", "P1", "P2", "P3"]


def _upgrade(level: str) -> str:
    i = _LEVEL_ORDER.index(level) if level in _LEVEL_ORDER else 2
    return _LEVEL_ORDER[max(i - 1, 0)]


# 现场口语 -> 故障谱里的标准说法。
# 只做"同一件事换个说法"的归一，不新增故障类型：归一之后仍然交给
# fault_spectrum.infer_from_text 判等级与 SOP，保证平台和内核口径一致。
_NORMALIZE = (
    (("压缩机", "机组停", "机组坏", "不制冷", "制冷坏", "冷机"), "制冷机故障"),
    (("厢温升", "厢温高", "温度升高", "温度过高", "超温", "失温", "温度超"), "温度超标"),
    (("追尾", "翻车", "撞车", "碰撞", "事故"), "交通事故"),
    (("轮胎爆", "胎爆", "爆了胎", "爆胎"), "车辆故障"),
    (("坏车", "抛锚", "车坏了", "车辆故障"), "车辆故障"),
    (("厢门没关", "车门没关", "门没关严", "门没关好"), "厢门未关"),
    (("走错路", "绕路", "偏离路线", "偏航"), "偏航"),
    (("堵车", "塞车", "拥堵", "堵住"), "拥堵"),
    (("封路", "道路封闭", "交通管制"), "道路封闭"),
    (("暴雨", "大雨"), "暴雨"),
    (("大雪", "暴雪"), "暴雪"),
    (("台风", "飓风"), "台风"),
    (("大雾", "能见度低"), "大雾"),
    (("扣车", "被扣", "查验", "海关查"), "海关查验"),
    (("泄漏", "泄露", "洒漏"), "泄漏"),
    (("破损", "压坏", "摔坏"), "货物破损"),
)


def _normalize(text: str) -> str:
    """把口语归一成故障谱能认的说法；已命中的不重复追加。"""
    t = text or ""
    extra = []
    for words, canon in _NORMALIZE:
        if any(w in t for w in words) and canon not in t:
            extra.append(canon)
    return (t + " " + " ".join(extra)).strip() if extra else t


def _impact(order: Optional[ShipperOrder], level: str, name: str) -> str:
    """影响范围只用订单里真实存在的字段描述。"""
    if order is None:
        return f"{name}，等级 {level}；订单信息缺失，需人工补充。"
    weight = f"{float(order.weight_t):g} 吨" if order.weight_t is not None else "吨位未填"
    parts = [f"{order.origin or '起运地未填'} → {order.dest or '目的地未填'} 的 "
             f"{order.cargo_type or '货物'}（{weight}）"]
    if order.required_by:
        parts.append(f"要求 {order.required_by} 前送达")
    if order.carrier_org_id:
        parts.append("承运商已接单，处置不换承运商")
    policy = LEVEL_POLICY.get(level, LEVEL_POLICY["P2"])
    parts.append(f"{level} 级，要求 {policy['sla_min']} 分钟内完成处置")
    return "；".join(parts)


def classify(description: str, order: Optional[ShipperOrder] = None) -> Dict[str, Any]:
    """把一段异常描述分级。未命中故障谱但含异常线索 -> E-UNKNOWN P2（不编造具体故障）。"""
    text = (description or "").strip()
    cargo = getattr(order, "cargo_type", "") or ""
    norm = _normalize(text)
    inf = infer_from_text(f"{norm} {cargo}".strip())

    if inf.get("matched"):
        code = inf["code"]
        name = inf["name"]
        level = inf.get("level") or "P2"
        kw = inf.get("keyword")
        matched = True
    else:
        hint = has_anomaly_hint(norm)
        if not hint:
            return {"matched": False, "code": None, "name": None, "level": None,
                    "sop_id": None, "impact": "", "sla_min": None,
                    "approval": False, "policy": None,
                    "note": "描述里没有异常线索，按常规运输处理，不进入处置链。"}
        code, name, level, kw = "E-UNKNOWN", "未识别异常·需人工确认", "P2", hint
        matched = False

    # 高价值/高敏感货类往上抬一档，避免"温度漂移"在医药货上按 P1 处置
    if cargo in _ESCALATE_CARGO and code and code.startswith(("E-COLD", "E-DG", "E-SLA")):
        level = _upgrade(level)

    policy = LEVEL_POLICY.get(level, LEVEL_POLICY["P2"])
    return {"matched": matched, "code": code, "name": name, "level": level,
            "sop_id": by_code(code).sop_id if by_code(code) else sop_for(code or ""),
            "keyword": kw, "impact": _impact(order, level, name),
            "sla_min": policy["sla_min"], "approval": policy["approval"],
            "policy": policy, "note": ""}


def suggest(description: str, order: Optional[ShipperOrder] = None,
            top_k: int = 2) -> Dict[str, Any]:
    """分级 + 检索 SOP，返回给调度员看的完整建议（不执行任何动作）。"""
    tri = classify(description, order)
    hits: List[Dict[str, Any]] = []
    if tri.get("code"):
        try:
            from sca.knowledge_base import search_sop

            rows = search_sop(
                f"{_normalize(description)} {(order.cargo_type if order else '') or ''}".strip(),
                top_k=top_k, code=tri["code"])
            hits = [{"sop_id": h.get("sop_id"), "title": h.get("title"),
                     "level": h.get("level"), "match": h.get("match"),
                     "steps": h.get("steps") or [],
                     "owner": h.get("owner"), "notify": h.get("notify"),
                     "channel": h.get("channel")} for h in rows]
        except Exception:  # noqa: BLE001 知识库不可用不阻塞分级
            hits = []
    return {"triage": tri, "sop": hits}


def triage_existing(db: Session, e: ExceptionReport) -> Dict[str, Any]:
    """对已上报的异常做一次分级（不改库，只返回结论）。"""
    order = db.get(ShipperOrder, e.order_id)
    return suggest(e.description or "", order)


def resolve(db: Session, e: ExceptionReport, user: Dict[str, Any],
            action: str, resolution: str, ip: str = "",
            delay_h: Optional[float] = None,
            reason_code: Optional[str] = None,
            new_carrier_org_id: Optional[int] = None) -> Dict[str, Any]:
    """处置异常。

    支持的动作：
      * `resume`   —— 异常排除，恢复在途（不动路线）
      * `replan`   —— **真实改道**：把受影响城市从途经点剔除后重新规划，写新方案版本；
                      定位不到受影响城市时诚实降级为 `replan_skipped`，不编造绕行里程
      * `extend`   —— 独立延长承诺时效（见 delay_h），并通知货主
      * `deliver`  —— 货物已交付，异常收口
      * `reassign` —— 有条件换承运商（见 services/reassign.py 的 5 个触发码），
                      必须给 `reason_code` 且原因 ≥10 字，属于强制审批项
    """
    if e.status == "resolved":
        raise ValueError("该异常已处置")
    if action not in ("resume", "replan", "extend", "deliver", "reassign"):
        raise ValueError("后续动作只能是 resume / replan / extend / deliver / reassign")
    resolution = (resolution or "").strip()
    if len(resolution) < 2:
        raise ValueError("请填写处置结论（至少 2 个字）")

    order = db.get(ShipperOrder, e.order_id)
    if not order:
        raise ValueError("订单不存在")

    tri = classify(e.description or "", order)
    action_used = action
    reroute_info: Optional[Dict[str, Any]] = None
    deadline_info: Optional[Dict[str, Any]] = None

    # ---- 改派：条件驱动，独立收口（reassign 内部会改订单状态并提交）----
    if action == "reassign":
        from . import reassign as _reassign

        info = _reassign.reassign(
            db, order, reason_code=reason_code or "", reason=resolution,
            user=user, new_carrier_org_id=new_carrier_org_id, ip=ip)
        e.status = "resolved"
        e.resolution = resolution
        e.resolved_by_user_id = user["user_id"]
        e.resolved_at = datetime.now()
        e.level = tri.get("level") or e.level
        e.exception_code = e.exception_code or tri.get("code")
        e.exception_name = e.exception_name or tri.get("name")
        e.resolution_action = "reassign"
        db.commit()
        return {"exception_id": e.id, "status": e.status,
                "order_status": order.status, "level": e.level,
                "code": e.exception_code, "action": "reassign",
                "reassign": info, "note": info.get("note")}

    # ---- 改道：真的重算，而不是只写一个 action 字段 ----
    if action == "replan":
        from . import reroute as _reroute

        pv = _reroute.preview_reroute(
            db, order, position_text=e.position_text or "",
            description=e.description or "")
        if pv.get("ok"):
            reroute_info = _reroute.apply_reroute(
                db, order, pv, reason=resolution, user=user, ip=ip)
        else:
            # 没有可定位的受影响路段就不假装改过：如实记 skipped 与原因
            action_used = "replan_skipped"
            reroute_info = {"skipped": True, "reason": pv.get("reason")}

    # ---- 改期：独立动作，只延后不提前 ----
    if action == "extend":
        from . import deadline as _deadline

        deadline_info = _deadline.extend(db, order, delay_h=delay_h,
                                        reason=resolution, user=user, ip=ip)

    e.status = "resolved"
    e.resolution = resolution
    e.resolved_by_user_id = user["user_id"]
    e.resolved_at = datetime.now()
    # 分级结果落到异常单上，复盘时不用再算一次
    e.level = tri.get("level") or e.level
    e.exception_code = e.exception_code or tri.get("code")
    e.exception_name = e.exception_name or tri.get("name")
    e.resolution_action = action_used
    e.deadline_shift_h = (deadline_info or {}).get("delay_h")

    target = {"resume": "in_transit", "replan": "in_transit",
              "extend": "in_transit", "deliver": "delivered"}[action]
    if order.status in ("exception", "in_transit"):
        try:
            transition(db, order, target, "exception_resolved", "dispatcher",
                       user["user_id"], {"exception_id": e.id, "action": action_used,
                                         "resolution": resolution,
                                         "level": e.level,
                                         "carrier_kept": order.carrier_org_id,
                                         **({"reroute": reroute_info}
                                            if reroute_info else {}),
                                         **({"deadline": deadline_info}
                                            if deadline_info else {})})
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    audit.log(db, user, "exception_resolved", target_type="exception", target_id=e.id,
              reason=resolution,
              detail={"order_no": order.order_no, "level": e.level,
                      "code": e.exception_code, "action": action_used,
                      **({"detour_km": reroute_info.get("detour_km")}
                         if reroute_info else {}),
                      **({"delay_h": deadline_info.get("delay_h")}
                         if deadline_info else {})}, ip=ip)
    db.commit()
    out = {"exception_id": e.id, "status": e.status, "order_status": order.status,
           "level": e.level, "code": e.exception_code, "action": action_used,
           "note": "承运商未变更（异常处置不换承运商）"}
    if reroute_info:
        out["reroute"] = reroute_info
        out["note"] = (reroute_info.get("note") if not reroute_info.get("skipped")
                       else f"未改道：{reroute_info.get('reason')}")
    if deadline_info:
        out["deadline"] = deadline_info
        out["note"] = deadline_info.get("note") or out["note"]
    return out


def open_exceptions(db: Session, limit: int = 20) -> List[Dict[str, Any]]:
    """待处置异常 + 分级结论，按等级排序（P0 在最前）。"""
    rows = db.execute(select(ExceptionReport).where(
        ExceptionReport.status != "resolved").order_by(
        ExceptionReport.id.desc()).limit(limit)).scalars().all()
    out = []
    for e in rows:
        order = db.get(ShipperOrder, e.order_id)
        tri = classify(e.description or "", order)
        out.append({"id": e.id, "order_id": e.order_id,
                    "order_no": order.order_no if order else None,
                    "cargo_type": order.cargo_type if order else None,
                    "position_text": e.position_text,
                    "description": e.description,
                    "reported_at": e.created_at.isoformat(timespec="seconds")
                    if e.created_at else None,
                    "triage": tri})
    out.sort(key=lambda x: _LEVEL_ORDER.index(x["triage"].get("level") or "P3"))
    return out


def level_label(level: Optional[str]) -> str:
    return {"P0": "P0 紧急", "P1": "P1 重大", "P2": "P2 一般", "P3": "P3 轻微"}.get(
        level or "", level or "未分级")


def status_can_resolve(order: Optional[ShipperOrder]) -> bool:
    return bool(order) and order.status in ("exception", "in_transit")


__all__ = ["LEVEL_POLICY", "classify", "suggest", "triage_existing", "resolve",
           "open_exceptions", "level_label", "status_can_resolve", "STATUS_LABEL"]
