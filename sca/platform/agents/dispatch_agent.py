"""智能调度 Agent：调度台上那个"会办事、也能解释"的助手。

它不是一个只会聊天的壳子。链路与调度内核一致，只是把节点显式说出来：

    意图路由 → 订单上下文 → 规划/分级 → 审批闸口 → 执行（挂牌/处置）→ 复盘建议

三条设计约束（改之前先想清楚）：
  1. **写操作一律先给方案再等确认**。出方案、挂牌、处置异常都会改外部可见状态，
     直接执行等于把"确认"这一步删了；`_pending` 就是这个闸门。
  2. **数字只有一个来源**。价格、里程、收益全部来自 `services.scheduling` /
     `services.pricing`，与页面按钮同一份实现，Agent 不自己算。
  3. **答不出来就说答不出来**。知识库没命中异常处置 SOP 时只说通用流程，
     不编造具体步骤；订单没填的字段不替它补。

会话复用 chat_session 表（scene=dispatch_ops），所以刷新页面还能接着聊。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (ChatMessage, ChatSession, ExceptionReport, OrderEvent,
                      OrderPlan, OrderPublish, ShipperOrder)
from ..services import advice, scheduling, triage
from ..services.orders import STATUS_LABEL, order_brief
from . import slots as S
from . import views

_ORDER_NO = re.compile(r"(SO\d{8,20})", re.I)
_CN_INDEX = re.compile(r"第([一二两三四五六七八九十\d]+)单")

_ACT_PLAN = re.compile(r"出方案|生成方案|做方案|排个方案|规划一下|算一下路线|重新规划|优化路线")
_ACT_PUBLISH = re.compile(r"挂牌|发布|挂出去|推到订单池|定价")
_ACT_RESOLVE = re.compile(r"处置|处理掉|解决了|恢复运输|继续运输|完成交付|确认送达")
_ACT_ROLLBACK = re.compile(r"回退|退回|返工")
_ACT_EXC_LIST = re.compile(r"异常|告警|故障")
# "有什么异常 / 待处置的异常"是查看，不是执行；这两个词的组合优先按清单处理
_ACT_EXC_HINT = re.compile(r"有什么|哪些|清单|列表|几条|待处置|看看|列出|汇总|多少条")
_ACT_OVERVIEW = re.compile(r"概览|总览|今天|整体|多少单|情况怎么样|运行情况|统计|看板|指标")
_ACT_ORDER = re.compile(r"这单|这票|订单情况|订单详情|看一下|查看|详情|进度")

_LEVEL_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,.2f}{unit}" if isinstance(v, float) else f"{v}{unit}"
    return str(v)


class DispatchAgent:
    SCENE = "dispatch_ops"
    # 调度助手是常驻工具，不能像下单会话那样 12 轮就掐断
    MAX_ROUNDS = 200

    # ------------------------------------------------------------------ 会话
    def open_session(self, db: Session, user: Dict[str, Any],
                     session_id: Optional[str] = None) -> ChatSession:
        if session_id:
            s = db.get(ChatSession, session_id)
            if s and s.user_id == user["user_id"] and s.scene == self.SCENE:
                return s
        s = ChatSession(id=uuid.uuid4().hex, user_id=user["user_id"],
                        scene=self.SCENE, state="idle", slots={}, rounds=0)
        db.add(s)
        db.commit()
        return s

    # ------------------------------------------------------------------ 入口
    def handle(self, db: Session, user: Dict[str, Any], text: str,
               session_id: Optional[str] = None) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"session_id": session_id or "", "reply": "说点什么，我这边好办事。",
                    "state": "idle", "actions": [], "trace": []}
        s = self.open_session(db, user, session_id)
        slots: Dict[str, Any] = dict(s.slots or {})
        s.rounds = int(s.rounds or 0) + 1
        if s.rounds > self.MAX_ROUNDS:
            db.commit()
            return self._resp(s, "这个会话开得太久了，点右上角「新会话」重新开始更清爽。",
                              slots=slots)

        self._log(db, s, "user", text)
        trace: List[str] = []
        pending = slots.get("_pending") or {}

        # ---- 1. 闸口：等待确认的动作 ----
        if pending:
            if S.is_affirmative(text):
                return self._execute(db, s, user, slots, pending, trace)
            if S.is_negative(text):
                slots.pop("_pending", None)
                s.slots = slots
                s.state = "idle"
                db.commit()
                return self._resp(s, "好，这个动作撤回了，什么都没改。要换成别的做法直接说。",
                                  slots=slots, trace=["审批闸口：已撤销"])

        # ---- 2. 订单上下文 ----
        order, focus_note = self._focus(db, slots, text, trace)

        # ---- 3. 意图路由 ----
        intent = self._intent(text, bool(order))
        trace.append(f"意图路由：{self._intent_label(intent)}")

        if intent == "overview":
            return self._overview(db, s, slots, trace)

        if intent == "exceptions":
            return self._exceptions(db, s, slots, order, text, trace)

        if intent == "plan":
            return self._need_order(db, s, slots, order, trace) or \
                self._propose_plan(db, s, slots, order, trace)

        if intent == "publish":
            return self._need_order(db, s, slots, order, trace) or \
                self._propose_publish(db, s, slots, order, trace)

        if intent == "resolve":
            return self._resolve(db, s, slots, order, text, trace)

        if intent == "rollback":
            return self._need_order(db, s, slots, order, trace) or \
                self._propose_rollback(db, s, slots, order, text, trace)

        if intent == "order":
            if not order:
                return self._need_order(db, s, slots, None, trace)
            return self._order_card(db, s, slots, order, trace)

        if intent == "knowledge":
            return self._knowledge(db, s, slots, order, text, trace)

        # ---- 4. 兜底：不像指令就当问题答，答不出就给可用动作 ----
        q = advice.answer(text, order, include_budget=True,
                          fee_ctx=self._fee_ctx(db, order))
        if q.get("kind") != "none" and q.get("text"):
            return self._say(db, s, slots, q["text"], trace)
        return self._help(db, s, slots, order, focus_note, trace)

    # ------------------------------------------------------------------ 意图
    def _intent(self, text: str, has_order: bool) -> str:
        if _ACT_PLAN.search(text):
            return "plan"
        if _ACT_PUBLISH.search(text):
            return "publish"
        if _ACT_ROLLBACK.search(text):
            return "rollback"
        if _ACT_EXC_LIST.search(text) and _ACT_EXC_HINT.search(text):
            return "exceptions"
        if _ACT_RESOLVE.search(text):
            return "resolve"
        if _ACT_OVERVIEW.search(text):
            return "overview"
        if _ACT_EXC_LIST.search(text):
            return "exceptions"
        if _ORDER_NO.search(text) or (_ACT_ORDER.search(text) and has_order):
            return "order"
        if advice.classify_question(text) != "none":
            return "knowledge"
        if has_order:
            return "order"
        return "unknown"

    @staticmethod
    def _intent_label(intent: str) -> str:
        return {"plan": "出方案", "publish": "定价挂牌", "resolve": "处置异常",
                "rollback": "回退重处理", "exceptions": "异常清单",
                "overview": "运行概览", "order": "订单解读",
                "knowledge": "订单知识问答", "unknown": "未识别（给可用动作）"}.get(intent, intent)

    # ------------------------------------------------------------------ 上下文
    def _recent(self, db: Session, limit: int = 6) -> List[ShipperOrder]:
        rows = db.execute(select(ShipperOrder).where(
            ShipperOrder.status != "draft").order_by(
            ShipperOrder.id.desc()).limit(limit)).scalars().all()
        return list(rows)

    def _focus(self, db: Session, slots: Dict[str, Any], text: str,
               trace: List[str]) -> Tuple[Optional[ShipperOrder], str]:
        """确定"这单"指哪一单。显式订单号 > 第N单 > 上次聚焦。"""
        m = _ORDER_NO.search(text)
        if m:
            o = db.execute(select(ShipperOrder).where(
                ShipperOrder.order_no == m.group(1).upper())).scalars().first()
            if o:
                slots["_focus"] = o.id
                slots["_focus_card"] = self._card(db, o)
                trace.append(f"订单上下文：{o.order_no}")
                return o, "explicit"
            trace.append(f"订单上下文：订单号 {m.group(1)} 不存在")
            return None, "missing"

        idx = _CN_INDEX.search(text)
        if idx:
            n = S._CN_NUM.get(idx.group(1), None) if not idx.group(1).isdigit() \
                else int(idx.group(1))
            rows = self._recent(db)
            if n and 1 <= n <= len(rows):
                slots["_focus"] = rows[n - 1].id
                slots["_focus_card"] = self._card(db, rows[n - 1])
                trace.append(f"订单上下文：第 {n} 单 {rows[n - 1].order_no}")
                return rows[n - 1], "index"

        oid = slots.get("_focus")
        if oid:
            o = views.load_order(db, oid, "dispatch")
            if o:
                trace.append(f"订单上下文：沿用 {o.order_no}")
                return o, "kept"
        return None, "none"

    def _need_order(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                    order: Optional[ShipperOrder], trace: List[str]) -> Optional[Dict[str, Any]]:
        if order is not None:
            return None
        rows = [o for o in self._recent(db) if o.status in
                ("submitted", "planned", "published", "candidate", "claimed",
                 "in_transit", "exception")]
        actions = [{"label": f"{o.order_no}", "text": f"看一下 {o.order_no}"}
                   for o in rows[:4]]
        text = ("要动哪一单？发订单号（SO 开头），或者说「第一单」。"
                "\n当前进行中的单子：\n" +
                "\n".join(f"- {o.order_no}｜{o.origin}→{o.dest}｜"
                          f"{STATUS_LABEL.get(o.status, o.status)}" for o in rows[:5])
                if rows else "现在没有进行中的订单，等货主发布后我再帮你排。")
        return self._say(db, s, slots, text, trace, actions=actions)

    def _fee_ctx(self, db: Session, order: Optional[ShipperOrder]) -> Dict[str, Any]:
        if not order:
            return {}
        pub = db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id,
            OrderPublish.status == "open").order_by(
            OrderPublish.id.desc())).scalars().first()
        if not pub:
            return {"gross": float(order.budget_max) if order.budget_max else None}
        return {"gross": float(pub.gross_price), "shipper_fee": float(pub.shipper_fee),
                "publish": float(pub.publish_price),
                "carrier_fee_rate": float(pub.carrier_fee_rate)}

    # ------------------------------------------------------------------ 概览
    def _overview(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                  trace: List[str]) -> Dict[str, Any]:
        rows = db.execute(select(ShipperOrder)).scalars().all()
        cnt: Dict[str, int] = {}
        for o in rows:
            cnt[o.status] = cnt.get(o.status, 0) + 1
        excs = triage.open_exceptions(db, limit=10)
        resolved = db.execute(select(ExceptionReport).where(
            ExceptionReport.status == "resolved")).scalars().all()
        total_exc = len(resolved) + len(excs)
        self_heal = (len(resolved) / total_exc * 100) if total_exc else 0.0

        lines = [f"当前在系统里的订单 {len(rows)} 单："]
        for k in ("submitted", "planned", "published", "candidate", "claimed",
                  "in_transit", "exception", "delivered", "settled", "cancelled"):
            if cnt.get(k):
                lines.append(f"- {STATUS_LABEL.get(k, k)}：{cnt[k]} 单")
        lines.append(f"\n待处置异常 {len(excs)} 条；已处置 {len(resolved)} 条"
                     f"（自愈率 {self_heal:.0f}%）。")
        if excs:
            top = excs[0]
            tri = top["triage"]
            lines.append(f"最急的一条：{top['order_no']} {triage.level_label(tri.get('level'))}"
                         f"｜{tri.get('name')}｜{top['description'][:40]}")
            lines.append(f"按分级要求 {tri.get('sla_min')} 分钟内处置。")
        trace.append("复盘统计：按状态与异常等级汇总")

        actions = []
        if excs:
            actions.append({"label": "看异常清单", "text": "有什么待处置的异常"})
            actions.append({"label": "处置最急的一条", "text": "处置异常"})
        sub = [o for o in rows if o.status == "submitted"]
        if sub:
            actions.append({"label": f"给 {sub[0].order_no} 出方案",
                            "text": f"给 {sub[0].order_no} 出方案"})
        return self._say(db, s, slots, "\n".join(lines), trace, actions=actions,
                         extra={"overview": {"orders": cnt, "open_exceptions": len(excs),
                                             "resolved": len(resolved),
                                             "self_heal_rate": round(self_heal, 1)}})

    # ------------------------------------------------------------------ 异常
    def _exceptions(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                    order: Optional[ShipperOrder], text: str,
                    trace: List[str]) -> Dict[str, Any]:
        excs = triage.open_exceptions(db, limit=20)
        if order is not None:
            excs = [e for e in excs if e["order_id"] == order.id] or excs
        if not excs:
            return self._say(db, s, slots,
                             "现在没有待处置的异常。在途订单都正常。", trace,
                             actions=[{"label": "看运行概览", "text": "看一下整体情况"}])

        lines = [f"待处置异常 {len(excs)} 条，按等级排序："]
        for e in excs[:5]:
            tri = e["triage"]
            lines.append(
                f"- {triage.level_label(tri.get('level'))}｜{e['order_no']}｜"
                f"{tri.get('name') or '未分类'}\n"
                f"  位置：{e['position_text'] or '未报'}｜{e['description'][:50]}\n"
                f"  影响：{tri.get('impact')}\n"
                f"  要求 {tri.get('sla_min')} 分钟内处置"
                + ("，需人工审批" if tri.get("approval") else ""))
        top = excs[0]
        sop = triage.suggest(
            top["description"], views.load_order(db, top["order_id"], "dispatch"))
        trace.append(f"异常分级：{len(excs)} 条，最急 {top['triage'].get('level')}")
        if sop["sop"]:
            trace.append(f"SOP 检索：命中 {sop['sop'][0]['sop_id']}")
            lines.append("\n最急这条的建议方案：")
            lines.append(advice.render_sop(sop["sop"]))
        lines.append(f"\n要按建议处置 {top['order_no']}，回「处置 {top['order_no']}」；"
                     "也可以直接说你的处置结论。")
        slots["_last_exc"] = top["id"]
        s.slots = slots
        db.commit()
        actions = [{"label": f"处置 {top['order_no']}", "text": f"处置 {top['order_no']}"},
                   {"label": "看订单详情", "text": f"看一下 {top['order_no']}"}]
        return self._say(db, s, slots, "\n".join(lines), trace, actions=actions)

    def _resolve(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                 order: Optional[ShipperOrder], text: str,
                 trace: List[str]) -> Dict[str, Any]:
        """处置异常：先分级 -> 给方案 -> 等确认。"""
        e = self._pick_exception(db, order, slots)
        if not e:
            return self._say(db, s, slots,
                             "没有找到待处置的异常。如果异常已处理完，就不用再处置了。",
                             trace, actions=[{"label": "看异常清单", "text": "有什么待处置的异常"}])

        o = views.load_order(db, e.order_id, "dispatch")
        info = triage.suggest(e.description or "", o)
        tri = info["triage"]
        action = "deliver" if re.search(r"送达|交付|签收", text) else "resume"
        if re.search(r"改道|换路|重规划|绕行", text):
            action = "replan"

        # 用户自己给了结论就沿用，否则用 SOP 首步拼一个可执行的默认结论
        given = self._resolution_from_text(text)
        if given:
            resolution = given
        elif info["sop"] and info["sop"][0].get("steps"):
            resolution = f"按 {info['sop'][0]['sop_id']} 处置：" + \
                         "；".join(info["sop"][0]["steps"][:2])
        else:
            resolution = f"{tri.get('name') or '异常'}处置完成，恢复运输" if action != "deliver" \
                else "货物已送达，异常关闭"

        summary = (f"处置异常 #{e.id}（订单 {o.order_no if o else e.order_id}）\n"
                   f"- 分级：{triage.level_label(tri.get('level'))}"
                   f"｜{tri.get('name')}｜要求 {tri.get('sla_min')} 分钟内处置\n"
                   f"- 结论：{resolution}\n"
                   f"- 后续动作：{ {'resume': '恢复在途', 'replan': '改道后继续', 'deliver': '标记送达'}[action] }\n"
                   f"- 承运商保持不变（异常处置不换承运商）")
        if tri.get("approval"):
            summary += "\n\n这是高危等级，需要你确认后才执行。回「确认」执行，回「取消」放弃。"

        slots["_pending"] = {"action": "resolve", "exception_id": e.id,
                             "order_id": e.order_id, "code": action,
                             "resolution": resolution,
                             "summary": summary}
        s.slots = slots
        s.state = "confirming"
        db.commit()
        trace.append(f"异常分级：{tri.get('level')}｜{tri.get('code')}")
        trace.append("审批闸口：等待调度确认（高危处置）" if tri.get("approval")
                     else "审批闸口：等待确认")
        details = ""
        if info["sop"]:
            details = advice.render_sop(info["sop"])
        text_out = summary + ("\n\n知识库建议：\n" + details if details else "")
        return self._say(db, s, slots, text_out, trace,
                         actions=[{"label": "确认执行", "text": "确认"},
                                  {"label": "取消", "text": "取消"}])

    def _pick_exception(self, db: Session,
                        order: Optional[ShipperOrder],
                        slots: Dict[str, Any]) -> Optional[ExceptionReport]:
        q = select(ExceptionReport).where(ExceptionReport.status != "resolved")
        if order is not None:
            q = q.where(ExceptionReport.order_id == order.id)
        rows = db.execute(q.order_by(ExceptionReport.id.desc())).scalars().all()
        if rows:
            return rows[0]
        eid = slots.get("_last_exc")
        if eid:
            e = db.get(ExceptionReport, eid)
            if e and e.status != "resolved":
                return e
        return None

    @staticmethod
    def _resolution_from_text(text: str) -> str:
        """从"处置 结论是……"里取结论；只是指令词则返回空。"""
        m = re.search(r"(?:结论|处置为|处置成|按)\s*[:：]?\s*(.{4,200})", text)
        if m:
            return m.group(1).strip("。 ")
        t = re.sub(r"处置|处理|异常|一下|这单|订单|SO\d+", "", text).strip(" ，,。")
        return t if len(t) >= 6 else ""

    # ------------------------------------------------------------------ 出方案
    def _propose_plan(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                      order: ShipperOrder, trace: List[str]) -> Dict[str, Any]:
        p = scheduling.preview_plan(order, db)
        if not p.get("ok"):
            return self._say(db, s, slots, f"这单排不了：{p.get('reason')}。", trace)
        if order.status not in scheduling.PLANNABLE:
            return self._say(db, s, slots,
                             f"{order.order_no} 现在是「{STATUS_LABEL.get(order.status, order.status)}」，"
                             "这个阶段不用再出方案。", trace)

        wp = p.get("waypoints") or []
        route = " → ".join(wp) if wp else f"{order.origin} → {order.dest}"
        lines = [f"{order.order_no} 的规划结果（还没下发）：",
                 f"- 路线：{route}",
                 f"- 干线里程 {p['distance_km']:.1f} km，预计 {p['eta_h']:.1f} 小时",
                 f"- 干线成本 ¥{p['cost_est']:,.2f}（{p['cost_per_km']:.2f} 元/km）"]
        if p.get("publish_price") is not None:
            lines.append(f"- 若按预算 ¥{p['gross']:,.2f} 挂牌：扣货主侧 "
                         f"¥{p['shipper_fee']:,.2f}，挂牌价 ¥{p['publish_price']:,.2f}")
        if p.get("provider"):
            lines.append(f"- 路线来源：{p['provider']}")
        lines.append("\n确认后我会落库并把订单推进到「已出方案」。回「确认」执行，回「取消」放弃。")

        slots["_pending"] = {"action": "plan", "order_id": order.id,
                             "summary": "\n".join(lines)}
        s.slots = slots
        s.state = "confirming"
        db.commit()
        trace += [f"路径规划：{p['distance_km']:.0f} km / {p['eta_h']:.1f} h",
                  "审批闸口：等待调度确认"]
        return self._say(db, s, slots, "\n".join(lines), trace,
                         actions=[{"label": "确认", "text": "确认"},
                                  {"label": "取消", "text": "取消"},
                                  {"label": "看订单详情", "text": f"看一下 {order.order_no}"}])

    # ------------------------------------------------------------------ 挂牌
    def _propose_publish(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                         order: ShipperOrder, trace: List[str]) -> Dict[str, Any]:
        if order.status not in scheduling.PUBLISHABLE:
            hint = "得先出方案。" if order.status == "submitted" else \
                f"现在是「{STATUS_LABEL.get(order.status, order.status)}」，不需要挂牌。"
            return self._say(db, s, slots, f"{order.order_no} 还不能挂牌：{hint}", trace,
                             actions=[{"label": "出方案", "text": f"给 {order.order_no} 出方案"}]
                             if order.status == "submitted" else [])

        plan = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
        gross = float(order.budget_max or 0)
        if gross <= 0:
            return self._say(db, s, slots,
                             "这单货主没填预算，定不了价。先让货主补预算，或告诉我一个总价"
                             "（例如「按 8 万挂牌」）。", trace)
        from ..services import pricing as _pricing

        fr, cr = _fee_ctx_rates(db)
        amt = _pricing.settle_amounts(gross, fr, cr)
        warn = ""
        if plan and plan.cost_est and amt["publish"] < plan.cost_est:
            warn = (f"\n- 注意：挂牌价低于估算干线成本 ¥{plan.cost_est:,.2f}，"
                    "可能出现无人接单")

        lines = [f"{order.order_no} 的定价链路（还没挂牌）：",
                 f"- 货主预算 ¥{float(amt['gross']):,.2f}",
                 f"- 货主侧服务费 {float(amt['shipper_rate']) * 100:.2f}% = "
                 f"¥{float(amt['shipper_fee']):,.2f}",
                 f"- 挂牌价 ¥{float(amt['publish']):,.2f}（承运商看到的就是这个数）",
                 f"- 承运商侧费率 {float(amt['carrier_rate']) * 100:.2f}%（接单后从挂牌价扣）"
                 + warn,
                 "\n确认后挂牌，承运商订单池立即可见。回「确认」执行。"]
        slots["_pending"] = {"action": "publish", "order_id": order.id, "gross": gross,
                             "summary": "\n".join(lines)}
        s.slots = slots
        s.state = "confirming"
        db.commit()
        trace += [f"定价：挂牌价 ¥{float(amt['publish']):,.2f}",
                  "审批闸口：等待调度确认（挂牌对承运商可见）"]
        return self._say(db, s, slots, "\n".join(lines), trace,
                         actions=[{"label": "确认", "text": "确认"},
                                  {"label": "取消", "text": "取消"}])

    # ------------------------------------------------------------------ 回退
    def _propose_rollback(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                          order: ShipperOrder, text: str,
                          trace: List[str]) -> Dict[str, Any]:
        from ..services.orders import ROLLBACK

        targets = sorted(ROLLBACK.get(order.status, set()))
        if not targets:
            return self._say(db, s, slots,
                             f"{order.order_no} 当前「{STATUS_LABEL.get(order.status, order.status)}」"
                             "没有可回退的节点。", trace)
        target = next((t for t in targets if t in text), targets[0])
        reason = self._resolution_from_text(text)
        if not reason:
            slots["_pending"] = {"action": "rollback", "order_id": order.id,
                                 "target": target, "need_reason": True,
                                 "summary": f"回退 {order.order_no} 到「{STATUS_LABEL.get(target, target)}」"}
            s.slots = slots
            s.state = "confirming"
            db.commit()
            return self._say(db, s, slots,
                             f"可以回退到：{'、'.join(STATUS_LABEL.get(t, t) for t in targets)}。\n"
                             f"要把 {order.order_no} 退到「{STATUS_LABEL.get(target, target)}」，"
                             "请给我一句原因（会进系统日志）。", trace)
        slots["_pending"] = {"action": "rollback", "order_id": order.id,
                             "target": target, "reason": reason,
                             "summary": f"回退 {order.order_no} 到 "
                                        f"「{STATUS_LABEL.get(target, target)}」：{reason}"}
        s.slots = slots
        s.state = "confirming"
        db.commit()
        return self._say(db, s, slots,
                         f"确认把 {order.order_no} 回退到"
                         f"「{STATUS_LABEL.get(target, target)}」？原因：{reason}\n"
                         "回「确认」执行。", trace,
                         actions=[{"label": "确认", "text": "确认"},
                                  {"label": "取消", "text": "取消"}])

    # ------------------------------------------------------------------ 订单卡
    def _order_card(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                    order: ShipperOrder, trace: List[str]) -> Dict[str, Any]:
        brief = order_brief(order)
        plan = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
        pub = db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id).order_by(
            OrderPublish.id.desc())).scalars().first()
        excs = db.execute(select(ExceptionReport).where(
            ExceptionReport.order_id == order.id,
            ExceptionReport.status != "resolved")).scalars().all()

        lines = [advice.order_brief_text(order, include_budget=True)]
        if plan:
            lines.append(f"- 方案 v{plan.version}：{float(plan.distance_km or 0):.1f} km，"
                         f"{float(plan.eta_h or 0):.1f} 小时，成本 "
                         f"¥{float(plan.cost_est or 0):,.2f}")
        if pub:
            lines.append(f"- 挂牌：预算 ¥{float(pub.gross_price):,.2f} − 服务费 "
                         f"¥{float(pub.shipper_fee):,.2f} = ¥{float(pub.publish_price):,.2f}")
        if excs:
            tri = triage.classify(excs[0].description or "", order)
            lines.append(f"- 未闭环异常：{triage.level_label(tri.get('level'))}"
                         f"｜{tri.get('name')}｜{excs[0].description[:40]}")
        tips = advice.cargo_meta(order.cargo_type)
        lines.append(f"\n这票货的关键点：{tips['care'][0]}")

        actions = []
        if order.status == "submitted":
            actions.append({"label": "出方案", "text": f"给 {order.order_no} 出方案"})
        elif order.status == "planned":
            actions.append({"label": "挂牌", "text": f"把 {order.order_no} 挂牌"})
        if excs:
            actions.append({"label": "处置异常", "text": "处置异常"})
        actions.append({"label": f"{order.cargo_type} 注意事项",
                        "text": f"{order.cargo_type}有哪些注意事项"})
        trace.append("订单上下文：方案/挂牌/异常已汇总")
        return self._say(db, s, slots, "\n".join(lines), trace, actions=actions)

    # ------------------------------------------------------------------ 问答
    def _knowledge(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                   order: Optional[ShipperOrder], text: str,
                   trace: List[str]) -> Dict[str, Any]:
        q = advice.answer(text, order, include_budget=True,
                          fee_ctx=self._fee_ctx(db, order))
        if q.get("kind") != "none" and q.get("text"):
            if q.get("sop"):
                q["text"] += "\n\n" + advice.render_sop(q["sop"])
                trace.append("知识检索：命中 SOP")
            trace.append(f"知识问答：{q['kind']}")
            return self._say(db, s, slots, q["text"], trace)
        return self._help(db, s, slots, order, "", trace)

    def _help(self, db: Session, s: ChatSession, slots: Dict[str, Any],
              order: Optional[ShipperOrder], note: str,
              trace: List[str]) -> Dict[str, Any]:
        lines = ["我可以直接帮你办这些事：",
                 "- 出方案：说「给 SO… 出方案」，我会给路线、里程、时效和成本，确认后才落库",
                 "- 定价挂牌：说「把 SO… 挂牌」，我会把预算、服务费、挂牌价算给你看",
                 "- 处置异常：说「处置异常」或「温控超标怎么处理」，我给分级和 SOP 建议",
                 "- 看情况：说「看一下整体情况」或「这单怎么样」",
                 "- 问知识：例如「冷链和生鲜有什么区别」「医药冷链要注意什么」「服务费怎么算」"]
        if note == "missing":
            lines.insert(0, "没找到这个订单号，核对一下再发给我。\n")
        return self._say(db, s, slots, "\n".join(lines), trace,
                         actions=[{"label": "看运行概览", "text": "看一下整体情况"},
                                  {"label": "待处置异常", "text": "有什么待处置的异常"}])

    # ------------------------------------------------------------------ 执行
    def _execute(self, db: Session, s: ChatSession, user: Dict[str, Any],
                 slots: Dict[str, Any], pending: Dict[str, Any],
                 trace: List[str]) -> Dict[str, Any]:
        act = pending.get("action")
        # 写操作必须用真实 ORM 实例（views 返回的是只读代理），这处刻意不走 views
        # noqa: isolation
        order = db.get(ShipperOrder, pending.get("order_id")) if pending.get("order_id") else None
        slots.pop("_pending", None)
        s.slots = slots
        s.state = "idle"

        try:
            if act == "plan" and order:
                r = scheduling.create_plan(db, order, user)
                self._audit(db, user, "agent_plan", order, {"plan_id": r["plan_id"]})
                trace += [f"执行：生成方案 v{r['version']}",
                          f"状态推进：{STATUS_LABEL.get(r['status'], r['status'])}"]
                body = (f"方案已生成（v{r['version']}）：{r['distance_km']:.1f} km，"
                        f"预计 {r['eta_h']:.1f} 小时，成本 ¥{r['cost_est']:,.2f}。\n"
                        f"{order.order_no} 现在是「{STATUS_LABEL.get(r['status'], r['status'])}」。")
                actions = [{"label": "接着挂牌", "text": f"把 {order.order_no} 挂牌"},
                           {"label": "看订单详情", "text": f"看一下 {order.order_no}"}]

            elif act == "publish" and order:
                r = scheduling.publish_order(db, order, user, gross=pending.get("gross"))
                self._audit(db, user, "agent_publish", order,
                            {"publish_price": r["publish_price"]})
                trace += [f"执行：挂牌价 ¥{r['publish_price']:,.2f}",
                          "状态推进：已挂牌，承运商可见"]
                body = (f"已挂牌，承运商订单池可见：挂牌价 ¥{r['publish_price']:,.2f}"
                        f"（货主预算 ¥{r['gross']:,.2f}，服务费 ¥{r['shipper_fee']:,.2f}）。")
                if r.get("warning"):
                    body += f"\n注意：{r['warning']}"
                actions = [{"label": "看订单详情", "text": f"看一下 {order.order_no}"},
                           {"label": "看整体情况", "text": "看一下整体情况"}]

            elif act == "resolve":
                e = db.get(ExceptionReport, pending["exception_id"])
                if not e:
                    raise ValueError("异常记录不存在")
                r = triage.resolve(db, e, user, pending.get("code") or "resume",
                                   pending.get("resolution") or "处置完成",
                                   ip="")
                trace += [f"执行：异常 #{e.id} 已处置（{r.get('level')}）",
                          f"状态推进：{STATUS_LABEL.get(r['order_status'], r['order_status'])}"]
                body = (f"异常已处置：{triage.level_label(r.get('level'))}"
                        f"｜{r.get('code')}\n订单状态回到"
                        f"「{STATUS_LABEL.get(r['order_status'], r['order_status'])}」，承运商未变更。")
                actions = [{"label": "看订单详情", "text": f"看一下 {order.order_no}"}
                           if order else {"label": "看异常清单", "text": "有什么待处置的异常"}]

            elif act == "rollback" and order:
                from ..services.orders import rollback_order

                r = rollback_order(db, order, user, pending["target"],
                                   pending.get("reason") or "Agent 回退")
                trace.append(f"执行：回退到 {r['status']}")
                body = (f"已回退到「{STATUS_LABEL.get(r['status'], r['status'])}」，"
                        "原因已记入系统日志。")
                actions = []

            else:
                raise ValueError("这个动作的状态已经变了，请重新发起。")

        except (scheduling.SchedulingError, ValueError) as exc:
            db.rollback()
            s.slots = slots
            db.commit()
            trace.append("执行失败：业务规则拦截")
            return self._say(db, s, slots, f"没执行成功：{exc}", trace)

        s.slots = slots
        s.order_id = pending.get("order_id")
        db.commit()
        return self._say(db, s, slots, body, trace, actions=actions,
                         extra={"focus_order_id": pending.get("order_id")})

    def _audit(self, db: Session, user: Dict[str, Any], action: str,
               order: ShipperOrder, detail: Dict[str, Any]) -> None:
        """Agent 代调度员执行的动作也留痕，来源标成 agent，事后能分清谁做的。"""
        from ..services import audit

        audit.log(db, user, action, target_type="order", target_id=order.id,
                  detail={"by": "dispatch_agent", "order_no": order.order_no, **detail})
        db.commit()

    # ------------------------------------------------------------------ 输出
    def _say(self, db: Session, s: ChatSession, slots: Dict[str, Any], text: str,
             trace: List[str], actions: Optional[List[Dict[str, str]]] = None,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        oid = slots.get("_focus")
        if oid:
            o = views.load_order(db, oid, "dispatch")
            if o:
                slots["_focus_card"] = self._card(db, o)
            else:
                slots.pop("_focus", None)
                slots.pop("_focus_card", None)
        s.slots = slots
        db.commit()
        self._log(db, s, "agent", text, meta={"trace": trace, "actions": actions or []})
        return self._resp(s, text, slots=slots, trace=trace, actions=actions, extra=extra)

    def _resp(self, s: ChatSession, reply: str, slots: Optional[Dict[str, Any]] = None,
              trace: Optional[List[str]] = None,
              actions: Optional[List[Dict[str, str]]] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        slots = slots or {}
        data: Dict[str, Any] = {"trace": trace or [], "actions": actions or []}
        if extra:
            data.update(extra)
        data["focus"] = self._focus_payload(slots)
        return {"session_id": s.id, "state": s.state, "reply": reply,
                "slots": {k: v for k, v in slots.items() if not k.startswith("_")},
                "pending": bool(slots.get("_pending")), "rounds": int(s.rounds or 0),
                "actions": actions or [], "trace": trace or [], "data": data}

    def _card(self, db: Session, order: ShipperOrder) -> Dict[str, Any]:
        """右侧面板要的上下文卡片：只放调度员该看的字段。"""
        plan = db.get(OrderPlan, order.active_plan_id) if order.active_plan_id else None
        pub = db.execute(select(OrderPublish).where(
            OrderPublish.order_id == order.id).order_by(
            OrderPublish.id.desc())).scalars().first()
        exc = db.execute(select(ExceptionReport).where(
            ExceptionReport.order_id == order.id,
            ExceptionReport.status != "resolved")).scalars().all()
        return {
            "order_id": order.id, "order_no": order.order_no,
            "origin": order.origin, "dest": order.dest,
            "cargo_type": order.cargo_type,
            "weight_t": float(order.weight_t) if order.weight_t is not None else None,
            "temp_ctrl": bool(order.temp_ctrl), "hazmat": bool(order.hazmat),
            "required_by": order.required_by,
            "budget_max": float(order.budget_max) if order.budget_max is not None else None,
            "status": order.status,
            "status_label": STATUS_LABEL.get(order.status, order.status),
            "plan": {"version": plan.version,
                     "distance_km": float(plan.distance_km or 0),
                     "eta_h": float(plan.eta_h or 0),
                     "cost_est": float(plan.cost_est or 0)} if plan else None,
            "publish": {"publish_price": float(pub.publish_price),
                        "shipper_fee": float(pub.shipper_fee),
                        "gross": float(pub.gross_price)} if pub else None,
            "open_exceptions": len(exc),
            "level": (triage.classify(exc[0].description or "", order).get("level")
                      if exc else None),
        }

    @staticmethod
    def _focus_payload(slots: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return slots.get("_focus_card")

    def _log(self, db: Session, s: ChatSession, role: str, content: str,
             meta: Optional[Dict[str, Any]] = None) -> None:
        db.add(ChatMessage(session_id=s.id, user_id=s.user_id, role=role,
                           content=content[:4000], meta=meta))
        db.commit()


def _fee_ctx_rates(db: Session):
    from ..services.runtime import active_fee

    return active_fee(db)


agent = DispatchAgent()
