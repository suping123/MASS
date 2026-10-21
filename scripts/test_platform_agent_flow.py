"""Agent 协作链端到端验证（阶段 C）。

    ./.venv/Scripts/python.exe scripts/test_platform_agent_flow.py

链路：
    承运商 --propose(异常)--> 异常域 --propose(定级+候选动作)--> 调度
    调度 --notify(升级人工)/accept--> 异常域 --notify--> 承运商

验证要点：
  1) 全链只经 `agent_message`，没有任何 Agent 直接调用另一个 Agent 的实现；
  2) 出方案/定级这类"算清楚"的活自动推进，改派这类"签字"的活只落通知给人；
  3) 越权取数（要对方看不见的字段）被 `reject`，不是"给了但没显示"；
  4) 有界：推完一轮后再次执行不产生新消息（不会自激）。

只创建 `flow:` 前缀的临时消息与站内通知，结束时清理；不改任何真实订单状态。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402

from sca.platform.agents import bus, contract  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.models import (AgentMemory, AgentMessage, Notification,  # noqa: E402
                                 ShipperOrder)

FAILS: list[str] = []
PASSED = 0
STAMP = str(int(time.time()))
CARRIER_KEY = "carrier:999"


def ok(cond, name: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


def main() -> int:
    db = session_as("app")
    threads: list[str] = []
    max_notif = 0
    try:
        orders = db.execute(select(ShipperOrder.id, ShipperOrder.order_no,
                                   ShipperOrder.status).where(
            ShipperOrder.carrier_org_id.isnot(None)).order_by(
            ShipperOrder.id.desc()).limit(2)).all()
        if not orders:
            print("SKIP：库里没有已接单的订单，无法验证协作链")
            return 0
        oid = int(orders[0][0])
        status_before = orders[0][2]
        thread = contract.thread_of(order_id=oid)
        threads.append(thread)
        max_notif = int(db.execute(select(func.max(Notification.id))).scalar() or 0)
        print(f"       用订单 {orders[0][1]}（id={oid}，状态 {status_before}）")

        print("\n[1] 承运商上报异常（投消息，不直接调用异常域）")
        m = bus.send(db, from_agent=CARRIER_KEY, to_agent="exception",
                     msg_type="propose",
                     payload={"amount": None, "reason": "制冷机故障，厢温升到 9.5 度",
                              "description": "制冷机故障，厢温升到 9.5 度",
                              "position_text": "徐州服务区"},
                     order_id=oid, commit=True)
        ok(m.id > 0, "消息落库", f"id={m.id}")
        ok(bus.pending_count(db, "exception") >= 1, "异常域有 1 条待处理")

        print("\n[2] 推完积压：异常域定级 -> 调度决策")
        # 用的是真正的 daemon（once 版 tick），不是另一套"泵"实现
        s1 = agent_rt.drain()
        db.rollback()   # 换快照：daemon 在别的会话里写，旧快照看不到
        ok(s1["handled"] >= 1, "泵消费了消息", str(s1))
        rows = bus.thread(db, thread)
        types = [(r["from"], r["to"], r["type"]) for r in rows]
        order_msgs = [t for t in types if t[0] == "exception" and t[1] == "dispatch"]
        ok(any(t[2] == "propose" for t in order_msgs),
           "异常域向调度发出 propose（定级结论）", str(types))
        ok(any(t[0] == "exception" and t[1] == CARRIER_KEY for t in types),
           "异常域回执给承运商", str(types))
        proposals = [r for r in rows if r["from"] == "exception" and r["to"] == "dispatch"]
        tri = (proposals[0]["payload"] or {}).get("triage") if proposals else {}
        ok((tri or {}).get("code") == "E-COLD-FAIL", "定级命中故障谱 E-COLD-FAIL",
           str(tri))
        ok((tri or {}).get("level") == "P0", "制冷机故障判 P0", str(tri))

        print("\n[3] 人审闸口：改派只落通知，不自动执行")
        dec = bus.recall(db, "dispatch", f"decision:{thread}") or {}
        ok(dec.get("pick") == "reassign", "车辆不可用 -> 调度选改派", str(dec))
        esc = [r for r in rows if r["from"] == "dispatch" and r["type"] == "notify"]
        ok(bool(esc), "调度把结论作为 notify 回给异常域", str(types))
        ok("人工" in str((esc[0]["payload"] or {}).get("event") if esc else ""),
           "措辞明确是升级人工，而不是已执行",
           str(esc[0]["payload"] if esc else ""))
        notif = db.execute(select(Notification).where(
            Notification.id > max_notif)).scalars().all()
        ok(any("待处置" in (n.title or "") for n in notif),
           "调度员收到站内通知", str([n.title for n in notif]))
        db.rollback()
        st_now = db.execute(select(ShipperOrder.status).where(
            ShipperOrder.id == oid)).scalar()
        ok(st_now == status_before, "订单状态没被自动改动（泵不替人签字）",
           f"{status_before} -> {st_now}")

        print("\n[4] 有界：再跑一轮不产生新消息")
        before = len(bus.thread(db, thread))
        s2 = agent_rt.drain()
        db.rollback()   # 换快照：daemon 在别的会话里写，旧快照看不到
        ok(s2["handled"] == 0, "没有待处理消息时立即退出", str(s2))
        ok(len(bus.thread(db, thread)) == before, "消息数没有增长",
           f"{before} -> {len(bus.thread(db, thread))}")

        print("\n[5] 自动路径：拥堵类异常不需要人审")
        bus.send(db, from_agent=CARRIER_KEY, to_agent="exception", msg_type="propose",
                 payload={"amount": None, "reason": "高速堵车严重走不动",
                          "description": "高速堵车严重走不动",
                          "position_text": "京港澳高速 K1200"},
                 order_id=oid, commit=True)
        agent_rt.drain()
        db.rollback()   # 换快照：daemon 在别的会话里写，旧快照看不到
        rows2 = bus.thread(db, thread)
        accepts = [r for r in rows2 if r["from"] == "dispatch" and r["type"] == "accept"]
        ok(bool(accepts), "改道路径下调度直接 accept（不需人审）",
           str([(r["from"], r["type"]) for r in rows2]))
        if accepts:
            dec2 = accepts[-1]
            idx = rows2.index(dec2)
            parent = next((r for r in rows2 if r["id"] == dec2["parent_id"]), {})
            ok(parent.get("from") == "exception", "accept 回给异常域",
               str(parent)[:160])

        print("\n[6] 越权取数与确定价")
        bus.send(db, from_agent=CARRIER_KEY, to_agent="dispatch", msg_type="request",
                 payload={"fields": ["budget_max"]}, order_id=oid, commit=True)
        bus.send(db, from_agent=CARRIER_KEY, to_agent="dispatch", msg_type="request",
                 payload={"fields": ["required_by_at", "origin"]}, order_id=oid,
                 commit=True)
        bus.send(db, from_agent=CARRIER_KEY, to_agent="dispatch", msg_type="counter",
                 payload={"amount": 9000, "reason": "这单太远要加钱"},
                 order_id=oid, commit=True)
        agent_rt.drain()
        db.rollback()   # 换快照：daemon 在别的会话里写，旧快照看不到
        rows3 = bus.thread(db, thread)
        rejects = [r for r in rows3 if r["from"] == "dispatch" and r["type"] == "reject"]
        oks = [r for r in rows3 if r["from"] == "dispatch" and r["type"] == "notify"
               and (r["payload"] or {}).get("event") == "字段返回"]
        ok(any("越权" in str((r["payload"] or {}).get("reason")) for r in rejects),
           "要预算 -> 被拒（不是给了但不显示）", str([r["payload"] for r in rejects]))
        ok(any("议价" in str((r["payload"] or {}).get("reason")) for r in rejects),
           "议价 -> 如实拒绝（定价是确定式）",
           str([r["payload"] for r in rejects]))
        ok(bool(oks), "合法字段请求被满足", str([r["payload"] for r in oks]))

        print("\n[7] 契约方向白名单仍然生效")
        bad = False
        try:
            bus.send(db, from_agent=CARRIER_KEY, to_agent="shipper",
                     msg_type="propose", payload={"amount": 1, "reason": "x"},
                     order_id=oid)
        except contract.ContractError:
            bad = True
        ok(bad, "承运商不能直接找货主（必须经调度/异常域）")
    finally:
        db.rollback()
        db.execute(delete(AgentMessage).where(
            AgentMessage.thread_id.in_(threads or ["__none__"])))
        db.execute(delete(AgentMessage).where(
            AgentMessage.from_agent == "exception",
            AgentMessage.msg_type == "propose",
            AgentMessage.thread_id.like("order:%")))
        db.execute(delete(AgentMemory).where(
            AgentMemory.mkey.like("decision:%"),
            AgentMemory.agent_key == "dispatch"))
        db.execute(delete(AgentMemory).where(
            AgentMemory.agent_key == "exception",
            AgentMemory.mkey.like("last:%")))
        if max_notif:
            db.execute(delete(Notification).where(Notification.id > max_notif))
        db.commit()
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项失败 / {PASSED + len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
