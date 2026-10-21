"""货主-订单 Agent：对话式收单。

状态机（三态，边界清晰）：
    collecting ──槽位齐全──> confirming ──用户确认──> submitted
        ^                        │
        └──── 用户说要改 ─────────┘

为什么必须先 confirming 再落单：槽位都是正则/规则抽出来的，"起运地抽错成隔壁市"
这类错误机器自己发现不了，只有让用户看一眼清单才能拦住。而且落单之后要进调度、
要计价、要派单，改一次的成本远高于此时多问一句。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import ChatMessage, ChatSession, ShipperOrder
from ..services import advice
from . import bus, handoff, slots as S
from . import views


def _wire_slots(slots: Dict[str, Any]) -> Dict[str, Any]:
    """只把业务槽位放进消息，并保证是 JSON 可序列化的值。"""
    out: Dict[str, Any] = {}
    for k in S.SLOT_LABEL:
        v = slots.get(k)
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    out["_raw"] = str(slots.get("_raw") or "")[:2000]
    return out


def _public(slots: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (slots or {}).items() if not k.startswith("_")}


def _focus_order(db: Session, s: ChatSession) -> Optional[ShipperOrder]:
    """会话里已经落单的订单，用于回答"我这单到哪了"这类问题。"""
    if not s.order_id:
        return None
    return views.load_order(db, s.order_id, "shipper")


class ShipperAgent:
    SCENE = "shipper_order"

    # ------------------------------------------------------------------
    def open_session(self, db: Session, user: Dict[str, Any],
                     session_id: Optional[str] = None) -> ChatSession:
        if session_id:
            s = db.get(ChatSession, session_id)
            if s and s.user_id == user["user_id"] and s.scene == self.SCENE:
                return s
        s = ChatSession(id=uuid.uuid4().hex, user_id=user["user_id"],
                        scene=self.SCENE, state="collecting",
                        slots={}, rounds=0)
        db.add(s)
        db.commit()
        return s

    # ------------------------------------------------------------------
    def handle(self, db: Session, user: Dict[str, Any], text: str,
               session_id: Optional[str] = None) -> Dict[str, Any]:
        text = (text or "").strip()
        s = self.open_session(db, user, session_id)

        if s.state in ("submitted", "aborted"):
            # 会话已终结：不静默复用，明确告诉调用方开新会话
            return self._resp(s, "这个会话已结束，请开始新的下单会话。",
                              done=True, extra_state=s.state)

        s.rounds = int(s.rounds or 0) + 1
        if s.rounds > config.CHAT_MAX_ROUNDS:
            s.state = "aborted"
            db.commit()
            return self._resp(s, f"本次对话已进行 {config.CHAT_MAX_ROUNDS} 轮仍未收集齐信息，"
                                 "为避免反复占用时间，请重新发起下单。", done=True)

        slots: Dict[str, Any] = dict(s.slots or {})
        slots["_raw"] = (slots.get("_raw") or "") + " " + text

        self._log(db, s, "user", text)

        # ---- 问答：先回答，再继续收单 ----
        # 用户问"冷链要注意什么/运费怎么算"时，不能把这句话当成起运地塞进槽位。
        # 但"普货"这种裸词必须让位给槽位解析（advice.classify_question 里
        # looks_like_plain_answer 已把它判成 none），否则槽位永远填不上。
        if s.state in ("collecting", "confirming"):
            qa = advice.answer(text, _focus_order(db, s), include_budget=True)
            if qa.get("kind") != "none" and qa.get("text"):
                s.rounds = max(0, int(s.rounds or 0) - 1)   # 提问不占用轮次
                tail = ("\n\n（信息都还在，确认无误回「确认」即可）"
                        if s.state == "confirming" else
                        "\n\n（下单信息还没齐，接着说就行）")
                self._log(db, s, "agent", qa["text"] + tail)
                # options 给前端渲染成可点按钮（"有什么货类"的答案）
                return self._resp(s, qa["text"] + tail,
                                  data={"options": qa.get("options") or [],
                                        "kind": qa.get("kind")})

        # ---- 确认态 ----
        if s.state == "confirming":
            if S.is_affirmative(text):
                return self._submit(db, s, user, slots)
            if S.is_negative(text):
                s.state = "collecting"
                slots = S.apply_parse(slots, text, overwrite=True)
                return self._advance(db, s, slots, prefix="好的，已按你的说法更新。")
            # 既不是肯定也不是否定 -> 按"要修改"处理，比反复追问更符合直觉
            slots = S.apply_parse(slots, text, overwrite=True)
            return self._advance(db, s, slots, prefix="已按你的说法更新。")

        # ---- 收集态 ----
        # 用户说「不对/改成」或重述了完整路线 -> 允许覆盖；否则只补空位。
        # is_correction 覆盖「货类改成农产品」这类句中修正（句首不是否定词）。
        ow = S.is_negative(text) or S.explicit_route(text) or S.is_correction(text)
        slots = S.apply_parse(slots, text, overwrite=ow)
        return self._advance(db, s, slots)

    # ------------------------------------------------------------------
    def _advance(self, db: Session, s: ChatSession, slots: Dict[str, Any],
                 prefix: str = "") -> Dict[str, Any]:
        miss = S.missing_slots(slots)
        if not miss:
            s.state = "confirming"
            slots.pop("_asking", None)
            # 重新赋值而不是靠原地改：s.slots 是 JSON 列，只有赋值才标脏
            s.slots = dict(slots)
            db.commit()
            reply = (prefix + "\n\n" if prefix else "") + S.summary_text(slots)
            self._log(db, s, "agent", reply)
            return self._resp(s, reply, data={"summary": S.summary_rows(slots),
                                              "await_confirm": True})
        s.state = "collecting"

        # 同一项连续追问就换说法：第 2 次给可选值/示例。
        # 旧实现不论问几遍都是同一句，用户答两次没进展就不知道该答什么了。
        key = miss[0]
        asked = dict(slots.get("_asked") or {})
        n = int(asked.get(key) or 0) + 1
        asked[key] = n
        slots["_asked"] = asked
        slots["_asking"] = key
        s.slots = dict(slots)
        db.commit()

        qs = S.next_questions(slots)
        # 一次最多问 2 项，且明确说出还缺什么，避免用户不知道要答到什么时候
        lines = [(prefix + "\n\n" if prefix else "") + "还需要确认：" + " ".join(qs)]
        if n >= 2:
            hint = S.SLOT_HINT.get(key)
            if hint:
                lines.append("\n" + hint())
        lines.append(f"（还差 {len(miss)} 项：{'、'.join(S.SLOT_LABEL[k] for k in miss)}）")
        reply = "\n".join(lines)
        self._log(db, s, "agent", reply)
        data = {"asking": key}
        if key == "cargo_type" and n >= 2:
            from sca.cargo import CATEGORIES
            data["options"] = list(CATEGORIES)
        return self._resp(s, reply, data=data)

    # ------------------------------------------------------------------
    def _submit(self, db: Session, s: ChatSession, user: Dict[str, Any],
                slots: Dict[str, Any]) -> Dict[str, Any]:
        miss = S.missing_slots(slots)
        if miss:   # 理论上不会发生：confirming 只在齐全时进入
            s.state = "collecting"
            db.commit()
            return self._resp(s, "还缺信息：" + "、".join(S.SLOT_LABEL[k] for k in miss)
                              + "。请补充后再确认。")

        try:
            # 落单的执行者是常驻的货主 agent 进程（S5）。这里只传"业务槽位"，
            # 不把整个 slots 原样扔过去：里面的 `_intent` / `_quote` 是对话内部
            # 状态，落单用不上，混进消息里会让日志难读、也会把非 JSON 值带进库。
            r = handoff.result(handoff.send(
                db, user, action="create_order",
                args={"slots": _wire_slots(slots), "session_id": str(s.id)},
                # 落单时订单还不存在，thread 只能挂在会话上；挂牌之后
                # 才真正进入 order:{id} 这条线
                thread_id=f"chat:{s.id}"))
            s.state = "submitted"
            s.order_id = int(r["order_id"])
            s.slots = slots
            db.commit()
        except bus.NoReply as exc:
            # agent 没在跑 ≠ 单子有问题：会话保持 confirming，起来之后再说一次「确认」，
            # 落单侧按 session_id 幂等，不会重复建单。
            s.state = "confirming"
            db.commit()
            return self._resp(s, f"{exc}。请先双击 start_agents.bat 启动货主 agent，"
                                 "再回复「确认」重试。")
        except Exception as exc:  # noqa: BLE001 落单失败必须保持会话可继续
            db.rollback()
            s.state = "confirming"
            db.commit()
            return self._resp(s, f"提交失败（{type(exc).__name__}）：{exc}\n"
                                 "信息还在，可以稍后再回复「确认」重试。")

        order_no = r.get("order_no")
        reply = (f"已提交，订单号 {order_no}。调度方会尽快出方案并发布到承运商订单池，"
                 "你可以在「我的订单」里跟踪状态。")
        self._log(db, s, "agent", reply, meta={"order_id": s.order_id})
        return self._resp(s, reply, done=True, order_id=s.order_id)

    # ------------------------------------------------------------------
    def _resp(self, s: ChatSession, reply: str, done: bool = False,
              order_id: Optional[int] = None, extra_state: str = "",
              data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        slots = _public(s.slots or {})
        return {
            "session_id": s.id,
            "state": extra_state or s.state,
            "reply": reply,
            "slots": slots,
            "missing": S.missing_slots(slots),
            "rounds": int(s.rounds or 0),
            "done": done,
            "order_id": order_id or s.order_id,
            "data": data,
        }

    def _log(self, db: Session, s: ChatSession, role: str, content: str,
             meta: Optional[Dict[str, Any]] = None) -> None:
        db.add(ChatMessage(session_id=s.id, user_id=s.user_id, role=role,
                           content=content[:4000], meta=meta))
        db.commit()


agent = ShipperAgent()
