"""承运商-订单 Agent。

与货主端同一套纪律：缺信息就问、齐了先复述、确认才执行。
两个不可逆动作都走确认：**接单**（订单会被锁给这个承运商）和**报异常**（订单转入处置）。

接单要求承运商自报位置（替代车载 IoT）：位置是文本，系统不校验真实性，
但在复述里明确列出来让承运商自己核对——写错位置的责任边界由此划清。
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import ChatMessage, ChatSession, ShipperOrder
from ..services import advice, carrier_ops
from . import bus, handoff, slots as S
from . import views

logger = logging.getLogger("sca.platform.agents.carrier")

_EXC_WORDS = ("异常", "故障", "坏了", "不制冷", "堵", "事故", "抛锚", "追尾",
              "延误", "封路", "爆胎", "抛锚", "晚点", "车祸",
              # 天气类：司机报上来的原话通常是"暴雨走不动""路面积水"，
              # 缺这几个词会被判成"找单"，然后回一句"订单池没有可接的单"。
              "暴雨", "大雨", "暴雪", "大雪", "台风", "大雾", "积水", "风雪")
_ACCEPT_WORDS = ("接单", "我接", "接了", "要这单", "拿下", "可以做", "我来跑")
_QUOTE_WORDS = ("算", "收益", "赚", "利润", "多少钱", "划算")
_LIST_WORDS = ("有哪些", "列表", "货源", "有什么单", "看看单", "订单池")

_POS_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,14}?(?:服务区|收费站|出口|入口|路口|大桥|隧道|"
    r"路段|停车区|停车场|物流园|货场|附近|公里处))")
_ORDER_NO_RE = re.compile(r"(SO\d{8,})")


def _pos(text: str) -> Optional[str]:
    m = _POS_RE.search(text or "")
    return m.group(1) if m else None


class CarrierAgent:
    SCENE_ORDER = "carrier_query"
    SCENE_EXC = "carrier_exception"

    # ------------------------------------------------------------------
    def handle(self, db: Session, user: Dict[str, Any], text: str,
               session_id: Optional[str] = None) -> Dict[str, Any]:
        text = (text or "").strip()
        s = self._session(db, user, session_id)
        if s.state in ("submitted", "aborted"):
            return self._resp(s, "这个会话已结束，请重新发起。", done=True, state=s.state)

        s.rounds = int(s.rounds or 0) + 1
        if s.rounds > config.CHAT_MAX_ROUNDS:
            s.state = "aborted"
            db.commit()
            return self._resp(s, "对话轮次已达上限，请重新发起。", done=True)

        slots: Dict[str, Any] = dict(s.slots or {})
        self._log(db, s, "user", text)

        # ---- 问答：先答，再继续办正事 ----
        # "冷链能不能拉""这单收益怎么算"不是槽位，别把它当位置或描述存进去。
        oid = slots.get("order_id")
        qa = advice.answer(text, views.load_order(db, oid, "carrier"),
                           include_budget=False)
        if qa.get("kind") != "none" and qa.get("text"):
            s.rounds = max(0, int(s.rounds or 0) - 1)   # 提问不占用轮次
            tail = "\n\n（要接单或报异常，接着说要办哪一单就行）"
            self._log(db, s, "agent", qa["text"] + tail)
            return self._resp(s, qa["text"] + tail)

        # ---- 确认态：执行待办动作 ----
        if s.state == "confirming":
            if S.is_affirmative(text):
                return self._execute(db, s, user, slots)
            if S.is_negative(text):
                s.state = "collecting"
                slots.pop("_pending", None)
                db.commit()
                return self._resp(s, "好的，已取消。请重新告诉我该怎么处理。")
            # 其他输入当作补充信息
            return self._collect(db, s, user, slots, text)

        return self._collect(db, s, user, slots, text)

    # ------------------------------------------------------------------
    def _collect(self, db: Session, s: ChatSession, user: Dict[str, Any],
                 slots: Dict[str, Any], text: str) -> Dict[str, Any]:
        intent = slots.get("_intent") or self._intent(text)

        # 位置/描述随时可补
        p = _pos(text)
        if p and not slots.get("position_text"):
            slots["position_text"] = p
        m = _ORDER_NO_RE.search(text)
        if m:
            o = db.execute(select(ShipperOrder).where(
                ShipperOrder.order_no == m.group(1))).scalars().first()
            if not o:
                s.slots = slots
                db.commit()
                return self._resp(s, f"没找到订单号 {m.group(1)}，请核对后重发。")
            slots["order_id"] = o.id
            slots["order_no"] = o.order_no

        if intent == "exception":
            slots["_intent"] = "exception"
            if not slots.get("description"):
                rest = text
                if p:
                    rest = rest.replace(p, "")
                rest = rest.strip(" ，,。；;")
                if rest and not S.is_affirmative(rest):
                    slots["description"] = rest[:500]
            need = [k for k in ("order_id", "position_text", "description")
                    if slots.get(k) in (None, "")]
            if need:
                s.state = "collecting"
                s.slots = slots
                db.commit()
                q = {"order_id": "是哪一单？请发订单号（SO 开头）。",
                     "position_text": "你现在在哪个位置？请说到服务区/收费站/路段这一级。",
                     "description": "具体什么情况？"}
                ask = " ".join(q[k] for k in need[:2])
                self._log(db, s, "agent", ask)
                return self._resp(s, "还需要确认：" + ask, missing=need)
            return self._to_confirm(db, s, user, slots)

        if intent == "accept":
            slots["_intent"] = "accept"
            need = [k for k in ("order_id", "position_text") if slots.get(k) in (None, "")]
            if need:
                s.state = "collecting"
                s.slots = slots
                db.commit()
                q = {"order_id": "要接哪一单？请发订单号。",
                     "position_text": "接单后你从哪里出发？请给一个当前位置（文本即可）。"}
                self._log(db, s, "agent", " ".join(q[k] for k in need))
                return self._resp(s, "还需要确认：" + " ".join(q[k] for k in need), missing=need)
            return self._to_confirm(db, s, user, slots)

        if intent == "quote":
            if not slots.get("order_id"):
                s.slots = slots
                db.commit()
                return self._resp(s, "要算哪一单？请发订单号（例如 SO20261017000123）。")
            try:
                q = carrier_ops.quote(db, user, slots["order_id"])
            except ValueError as exc:
                s.slots = slots
                db.commit()
                return self._resp(s, f"试算失败：{exc}")
            s.slots = slots
            db.commit()
            return self._resp(s, self._quote_text(q), data=q)

        # list
        items = carrier_ops.pool(db, user.get("org_id"), limit=5)
        s.slots = slots
        if not items:
            db.commit()
            return self._resp(s, "公共订单池目前没有可接的单。")
        lines = [f"· {i['order_no']} {i['origin']}→{i['dest']} "
                 f"{i['cargo_type']} {i['weight_t']}吨 "
                 f"运费¥{i['publish_price']:,.0f}"
                 + (f"（预估收益¥{i['quick_profit']:,.0f}）" if i.get("quick_profit") is not None
                    else "")
                 for i in items]
        db.commit()
        return self._resp(s, "当前可接的订单：\n" + "\n".join(lines) +
                          "\n\n说「算一下 <订单号>」看收益，说「我接 <订单号>」开始接单。",
                          data={"pool": items})

    # ------------------------------------------------------------------
    def _to_confirm(self, db: Session, s: ChatSession, user: Dict[str, Any],
                    slots: Dict[str, Any]) -> Dict[str, Any]:
        intent = slots.get("_intent")
        if intent == "accept":
            try:
                q = carrier_ops.quote(db, user, slots["order_id"])
            except ValueError as exc:
                s.state = "collecting"
                s.slots = slots
                db.commit()
                return self._resp(s, f"暂时不能接这单：{exc}")
            slots["_quote"] = q
            s.state = "confirming"
            s.slots = slots
            db.commit()
            reply = (f"请确认接单：\n"
                     f"· 订单：{q['order_no']}（{q['order_status']}）\n"
                     f"· 里程：{q['distance_km']} km，单价 ¥{q['cost_per_km']}/km\n"
                     f"· 运费：¥{q['quoted_price']:,.2f}\n"
                     f"· 承运商侧服务费（{(q['carrier_fee'] / q['quoted_price'] * 100 if q['quoted_price'] else 0):.0f}%）："
                     f"¥{q['carrier_fee']:,.2f}\n"
                     f"· 预估成本：¥{q['est_cost']:,.2f}\n"
                     f"· **预估收益：¥{q['profit']:,.2f}**\n"
                     f"· 你的位置：{slots['position_text']}\n\n"
                     "确认接单请回复「确认」，否则回复「不」。")
            self._log(db, s, "agent", reply)
            return self._resp(s, reply, data={"summary": self._rows(q, slots),
                                              "await_confirm": True})

        # exception
        o = views.load_order(db, slots["order_id"], "carrier")
        s.state = "confirming"
        s.slots = slots
        db.commit()
        rows = [{"label": "订单", "value": slots.get("order_no") or "-"},
                {"label": "当前位置", "value": slots.get("position_text") or "-"},
                {"label": "异常情况", "value": slots.get("description") or "-"}]
        reply = ("请确认上报内容：\n" + "\n".join(f"· {r['label']}：{r['value']}" for r in rows) +
                 "\n\n确认提交请回复「确认」，要改直接说。")
        self._log(db, s, "agent", reply)
        return self._resp(s, reply, data={"summary": rows, "await_confirm": True})

    # ------------------------------------------------------------------
    def _execute(self, db: Session, s: ChatSession, user: Dict[str, Any],
                 slots: Dict[str, Any]) -> Dict[str, Any]:
        intent = slots.get("_intent")
        try:
            if intent == "accept":
                # 接单的实际执行者是常驻的承运商 agent 进程（S5）。
                # 对话这边只负责"人确认了"和"把结果说清楚"。
                r = handoff.result(handoff.send(
                    db, user, action="accept_order",
                    args={"order_id": slots["order_id"],
                          "position_text": slots.get("position_text") or ""},
                    order_id=int(slots["order_id"])))
                s.state = "submitted"
                db.commit()
                reply = (f"已接单：{r['order_no']}。位置已记录为「{r['position_text']}」，"
                         f"预估收益 ¥{r['profit']:,.2f}。")
                self._log(db, s, "agent", reply, meta={"order_id": r["order_id"]})
                return self._resp(s, reply, done=True, order_id=r["order_id"], data=r)

            r = carrier_ops.report_exception(db, user, slots["order_id"],
                                             slots.get("position_text") or "",
                                             slots.get("description") or "")
            s.state = "submitted"
            db.commit()
            # 上报的同时投一条消息给异常域：这是 Agent 协作链的起点。
            # 异常域只拿到"描述 + 位置 + 货类"，拿不到预算与报价（见 views 白名单）。
            # 消息发不出去不能挡住业务上报，所以单独 try。
            try:
                self._emit_exception(db, user, r, slots)
            except Exception as exc:  # noqa: BLE001
                logger.warning("异常消息投递失败（业务已上报）：%s", exc)
            reply = (f"异常已上报（单号 {r['order_no']}），调度方会尽快处置。"
                     "处置结果会通过平台消息同步给你。")
            self._log(db, s, "agent", reply, meta={"exception_id": r["exception_id"]})
            return self._resp(s, reply, done=True, order_id=r["order_id"], data=r)
        except bus.NoReply as exc:
            # 常驻进程没在跑：这不是"业务不行"，是"负责接单的那个 agent 不在"。
            # 说清楚该去启动什么，别让人以为是自己填错了。
            db.rollback()
            s.state = "confirming"
            db.commit()
            return self._resp(s, f"{exc}。请先双击 start_agents.bat 启动承运商 agent，"
                                 "再回复「确认」重试。")
        except ValueError as exc:
            db.rollback()
            s.state = "confirming"
            db.commit()
            return self._resp(s, f"执行失败：{exc}\n信息还在，可以改完再确认。")

    # ------------------------------------------------------------------
    def _emit_exception(self, db: Session, user: Dict[str, Any],
                        r: Dict[str, Any], slots: Dict[str, Any]) -> None:
        """把异常上报投进消息总线（carrier:<org> -> exception）。

        Agent 之间只走这条通道：异常域不会去读承运商的会话，也拿不到承运商的
        报价与收益；它只拿到描述、位置和被上报的订单号。
        """
        org = user.get("org_id")
        key = f"carrier:{org}" if org else "carrier"
        bus.send(db, from_agent=key, to_agent="exception", msg_type="propose",
                 payload={"amount": None,
                          "reason": slots.get("description") or "运输异常",
                          "description": slots.get("description") or "",
                          "position_text": slots.get("position_text") or "",
                          "exception_id": r.get("exception_id")},
                 order_id=r["order_id"], exception_id=r.get("exception_id"),
                 commit=True)

    # ------------------------------------------------------------------
    def _quote_text(self, q: Dict[str, Any]) -> str:
        return (f"订单 {q['order_no']} 收益试算：\n"
                f"· 运费（已扣货主侧服务费）：¥{q['quoted_price']:,.2f}\n"
                f"· 承运商侧服务费：¥{q['carrier_fee']:,.2f}\n"
                f"· 运输成本：{q['distance_km']} km × ¥{q['cost_per_km']}/km = "
                f"¥{q['est_cost']:,.2f}\n"
                f"· **预估收益：¥{q['profit']:,.2f}**\n"
                f"（口径：{q['formula']}）")

    def _rows(self, q: Dict[str, Any], slots: Dict[str, Any]):
        return [{"label": "订单", "value": q["order_no"]},
                {"label": "运费", "value": f"¥{q['quoted_price']:,.2f}"},
                {"label": "服务费", "value": f"¥{q['carrier_fee']:,.2f}"},
                {"label": "运输成本", "value": f"¥{q['est_cost']:,.2f}"},
                {"label": "预估收益", "value": f"¥{q['profit']:,.2f}"},
                {"label": "当前位置", "value": slots.get("position_text") or "-"}]

    def _intent(self, text: str) -> str:
        t = text or ""
        if any(w in t for w in _EXC_WORDS):
            return "exception"
        if any(w in t for w in _ACCEPT_WORDS):
            return "accept"
        if any(w in t for w in _QUOTE_WORDS):
            return "quote"
        if any(w in t for w in _LIST_WORDS):
            return "list"
        return "list"

    def _session(self, db: Session, user: Dict[str, Any],
                 session_id: Optional[str]) -> ChatSession:
        if session_id:
            s = db.get(ChatSession, session_id)
            if s and s.user_id == user["user_id"] and s.scene in (self.SCENE_ORDER, self.SCENE_EXC):
                return s
        s = ChatSession(id=uuid.uuid4().hex, user_id=user["user_id"],
                        scene=self.SCENE_ORDER, state="collecting", slots={}, rounds=0)
        db.add(s)
        db.commit()
        return s

    def _resp(self, s: ChatSession, reply: str, done: bool = False,
              order_id: Optional[int] = None, state: str = "",
              missing: Optional[list] = None,
              data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        slots = {k: v for k, v in (s.slots or {}).items() if not k.startswith("_")}
        return {"session_id": s.id, "state": state or s.state, "reply": reply,
                "slots": slots, "missing": missing or [],
                "rounds": int(s.rounds or 0), "done": done,
                "order_id": order_id or s.order_id, "data": data}

    def _log(self, db: Session, s: ChatSession, role: str, content: str,
             meta: Optional[Dict[str, Any]] = None) -> None:
        db.add(ChatMessage(session_id=s.id, user_id=s.user_id, role=role,
                           content=content[:4000], meta=meta))
        db.commit()


agent = CarrierAgent()
