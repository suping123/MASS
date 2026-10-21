"""时效延长：一个独立的处置动作，不是改道的副产品。

为什么必须独立
--------------
改造前，"时效变了"只能是改道算出来 ETA 变大后的副产物：`required_by` 字段改不了，
货主也不知道承诺被改写了。但现实里最常见的处置恰恰是"路不变，把承诺往后挪"——
比如目的地仓库压车、客户同意晚半天收货。这种情况下没有改道的理由，却有改期的需求。

三条硬规则
----------
1. **只延后不提前**：提前是订单变更（改单），走修改订单，不是异常处置。
2. **必须写原因**：承诺是给客户的契约，改动必须留痕，谁改的、为什么、改了多少。
3. **必须通知货主**：改期不通知等于没改——货主仍按老时间安排收货。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import OrderEvent, ShipperOrder
from . import audit, notify

MIN_DELAY_H = 0.5
MAX_DELAY_H = 72.0

_REL_DAYS = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "次日": 1,
             "今天": 0, "今日": 0, "当天": 0, "当日": 0}
_PM_WORDS = ("下午", "晚上", "傍晚", "夜里")


def parse_required_by(text: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """把货主写的时效文本解析成绝对时间；解析不出来返回 None（不猜）。

    支持：`后天下午6点前`、`明天 14:00 前`、`3天内`、`10月20日 18点`、`18:00前`。
    设计原则与内核一致：**认不出来就不给值**，绝不默认成"明天此刻"这种假时间——
    一个错的承诺时间比没有更糟，后面所有 SLA 判断都会跟着错。
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    now = now or datetime.now()

    # ① N 天内
    m = re.search(r"(\d+)\s*(?:个)?\s*(?:天|日|自然日)\s*内", s)
    if m:
        return (now + timedelta(days=int(m.group(1)))).replace(second=0, microsecond=0)

    # ② 时刻：6点 / 18:30 / 14时
    tmatch = re.search(r"(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|[点时])(?!\d)", s)
    hh = mm = None
    if tmatch:
        hh = int(tmatch.group(1))
        mm = int(tmatch.group(2) or 0)
        if not (0 <= hh <= 24 and 0 <= mm <= 59):
            hh = mm = None
    if hh is not None:
        if any(w in s for w in _PM_WORDS) and hh < 12:
            hh += 12
        elif "中午" in s and hh < 11:
            hh += 12
        if hh == 24:
            hh = 0

    # ③ 具体日期：10月20日 / 10-20
    dmatch = re.search(r"(\d{1,2})\s*[月\-/]\s*(\d{1,2})", s)
    base_date: Optional[date] = None
    if dmatch:
        mo, dd = int(dmatch.group(1)), int(dmatch.group(2))
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            year = now.year
            cand = date(year, mo, dd)
            if (cand - now.date()).days < -180:      # 已过去很久 -> 认为是明年
                cand = date(year + 1, mo, dd)
            base_date = cand
    # ②' 相对日
    if base_date is None:
        for word, off in _REL_DAYS.items():
            if word in s:
                base_date = now.date() + timedelta(days=off)
                break
    if base_date is None and hh is not None:
        base_date = now.date()
        if datetime.combine(base_date, time(hh, mm or 0)) <= now:
            base_date = base_date + timedelta(days=1)
    if base_date is None:
        return None
    if hh is None:
        hh, mm = 18, 0        # 只说了日期没说时刻：按当日 18:00 收口（行业惯例）
    return datetime.combine(base_date, time(hh, mm or 0))


def extend(db: Session, order: ShipperOrder, *, delay_h: Optional[float],
           reason: str, user: Dict[str, Any], ip: str = "") -> Dict[str, Any]:
    """把订单承诺时效往后延 `delay_h` 小时。返回改动前后的对比。"""
    if delay_h is None:
        raise ValueError("请给出要延长的时长（小时）")
    try:
        h = float(delay_h)
    except (TypeError, ValueError):
        raise ValueError("延长时间必须是数字（小时）") from None
    if h < MIN_DELAY_H or h > MAX_DELAY_H:
        raise ValueError(f"延长时长需在 {MIN_DELAY_H}–{MAX_DELAY_H} 小时之间")
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ValueError("请填写改期原因（至少 2 个字）")
    if not order.required_by_at:
        # 历史订单只有文本没有绝对时间：现场解析一次再改期。
        # 解析不出来就明确拒绝——没有承诺时间的"延长"没有意义。
        parsed = parse_required_by(order.required_by)
        if not parsed:
            raise ValueError("该订单没有可识别的承诺时效（只有自由文本或根本未填），"
                             "无法延长；请先补录准确的要求送达时间")
        order.required_by_at = parsed

    before = order.required_by_at
    after = before + timedelta(hours=h)
    old_text = order.required_by
    order.required_by_at = after
    order.required_by = after.strftime("%m-%d %H:%M 前")
    order.updated_at = datetime.now()

    seq = (db.execute(select(func.max(OrderEvent.seq)).where(
        OrderEvent.order_id == order.id)).scalar() or 0) + 1
    payload = {"delay_h": round(h, 2),
               "before": before.isoformat(timespec="minutes"),
               "after": after.isoformat(timespec="minutes"),
               "before_text": old_text, "reason": reason,
               # "超过原承诺"是这次动作的固有属性，显性记下来供统计
               "beyond_original": True}
    db.add(OrderEvent(order_id=order.id, seq=seq,
                      event_type="order_deadline_extended",
                      actor_role=user.get("role") or "dispatcher",
                      actor_user_id=user.get("user_id"), payload=payload))

    # 先落业务，再写通知：通知失败不应把已经改过的承诺回滚，但要让调用方看见
    db.flush()
    notify.push_order(
        db, order, ntype="deadline_changed",
        title=f"订单 {order.order_no} 送达时效已调整",
        body=(f"原因：{reason}\n原承诺：{before.strftime('%m-%d %H:%M')}\n"
              f"新承诺：{after.strftime('%m-%d %H:%M')}（延后 {h:g} 小时）"),
        to_shipper=True, to_carrier=True)

    audit.log(db, user, "order_deadline_extended", target_type="order",
              target_id=order.id, reason=reason,
              detail={"order_no": order.order_no, "delay_h": round(h, 2),
                      "before": before.isoformat(timespec="minutes"),
                      "after": after.isoformat(timespec="minutes")}, ip=ip)
    db.commit()
    return {"delay_h": round(h, 2),
            "before": before.isoformat(timespec="minutes"),
            "after": after.isoformat(timespec="minutes"),
            "before_text": old_text, "after_text": order.required_by,
            "note": f"时效已由 {before.strftime('%m-%d %H:%M')} 延至 "
                    f"{after.strftime('%m-%d %H:%M')}，已通知货主与承运商"}


__all__ = ["MIN_DELAY_H", "MAX_DELAY_H", "extend"]
