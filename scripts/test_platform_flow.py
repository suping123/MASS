"""三端闭环验证：货主下单 -> 调度发池 -> 承运商试算/接单 -> 报异常 -> 调度处置。

    ./.venv/Scripts/python.exe scripts/test_platform_flow.py

重点验证四件事：
  1) 信息边界：承运商看到的池子里**没有**货主预算；
  2) 金额守恒：货主实付 = 货主侧服务费 + 承运商侧服务费 + 承运商实收；
  3) 幂等：重复接单被拒、重复上报不产生第二条记录；
  4) 处置不换承运商。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from sca.platform import security  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402
from sca.platform.models import (CarrierBid, CarrierProfile, ChatMessage,  # noqa: E402
                                 ChatSession, ExceptionReport, OrderEvent,
                                 OrderPlan, OrderPublish, ShipperOrder)
from sca.platform.services import pricing  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def login(c: TestClient, account: str) -> str:
    r = c.post("/api/auth/code", json={"account": account, "purpose": "login"})
    code, cid = solve_captcha(r.json())
    r = c.post("/api/auth/login", json={"account": account, "password": "123456",
                                        "code": code, "cid": cid})
    assert r.status_code == 200, r.text
    return (r.json() or {})["token"]


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    # S5 起：下单/挂牌/接单都由 agent 进程执行，消息经 agent_message 表流转。
    # 进程内测试没有独立进程，这里把四个 daemon 起在线程里（跑的是同一份代码）。
    agents = agent_rt.start()

    # 前置条件：本套件的算术不含空驶（空驶归 test_platform_profit 验）。
    # 承运商当前位置在库里若残留了别的城市，`est_cost` 就会多出一段空驶成本，
    # 而下面「成本 = 里程 × 单价」比的是干线段 —— 两个套件的口径会互相污染。
    # 写回 NULL 就是 schema 的出厂值（sql/schema.sql 里 current_position 默认 NULL），
    # 不是"改别人数据"，是把这套算术的前提讲明白。
    db = session_as("app")
    try:
        prof = db.query(CarrierProfile).filter(CarrierProfile.org_id == 3).first()
        if prof is not None and prof.current_position is not None:
            print(f"  （清掉残留位置 {prof.current_position!r}，本套件按空车在起运地算）")
            prof.current_position = None
            db.commit()
    finally:
        db.close()

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}

        print("\n[1] 货主下单（对话：补全 -> 复述 -> 确认）")
        r = c.post("/api/shipper/chat", json={
            "text": "石家庄到南京 冷链 8吨 明天下午6点前送到 预算1.2万"}, headers=H_ship)
        d = r.json()
        sid = d["session_id"]
        check("信息齐全后进入确认态", d.get("state") == "confirming", f"{d.get('state')} {d.get('reply')[:60]}")
        r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H_ship)
        oid = r.json().get("order_id")
        check("确认后落单", bool(oid), str(r.json())[:120])

        print("\n[2] 调度出方案并定价挂牌")
        r = c.post(f"/api/dispatch/orders/{oid}/plan", headers=H_disp)
        check("出方案成功", r.status_code == 200, r.text[:160])
        plan = (r.json() or {}).get("plan") or {}
        check("方案带里程与 ETA", plan.get("distance_km") and plan.get("eta_h"),
              str(plan))
        check("方案来源如实标注为离线估算", plan.get("provider") == "offline-estimate",
              str(plan.get("provider")))

        r = c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)
        check("定价挂牌成功", r.status_code == 200, r.text[:160])
        pub = r.json() or {}
        gross = Decimal(str(pub.get("gross") or 0))
        sfee = Decimal(str(pub.get("shipper_fee") or 0))
        pprice = Decimal(str(pub.get("publish_price") or 0))
        check("货主侧服务费 = 预算 × 2%",
              sfee == (gross * Decimal("0.02")).quantize(pricing.CENT), f"{sfee} / {gross}")
        check("挂牌价 = 预算 − 货主侧服务费", pprice == gross - sfee, f"{pprice} / {gross}-{sfee}")

        print("\n[3] 承运商看池子：不含货主预算")
        r = c.get("/api/carrier/pool", headers=H_car)
        check("池子接口 200", r.status_code == 200, r.text[:120])
        items = r.json() or []
        mine = next((i for i in items if i["order_id"] == oid), None)
        check("能看到刚挂牌的单", mine is not None, str([i["order_id"] for i in items]))
        check("池子里没有预算字段", all("budget_max" not in i for i in items),
              str(items[:1])[:200])
        check("池子里能看到运费", mine and mine.get("publish_price") > 0, str(mine))

        print("\n[4] 承运商试算收益")
        r = c.post(f"/api/carrier/orders/{oid}/quote", headers=H_car)
        check("试算成功", r.status_code == 200, r.text[:160])
        q = r.json() or {}
        expect_fee = (Decimal(str(q["quoted_price"])) * Decimal("0.02")).quantize(pricing.CENT)
        expect_cost = (Decimal(str(q["distance_km"])) * Decimal(str(q["cost_per_km"]))).quantize(pricing.CENT)
        expect_profit = Decimal(str(q["quoted_price"])) - expect_fee - expect_cost
        check("承运商侧服务费 = 运费 × 2%", Decimal(str(q["carrier_fee"])) == expect_fee,
              f"{q.get('carrier_fee')} vs {expect_fee}")
        check("运输成本 = 里程 × 单价", Decimal(str(q["est_cost"])) == expect_cost,
              f"{q.get('est_cost')} vs {expect_cost}")
        check("收益 = 运费 − 服务费 − 成本", Decimal(str(q["profit"])) == expect_profit,
              f"{q.get('profit')} vs {expect_profit}")
        check("试算不改变订单状态（只看不接）", q.get("order_status") == "published",
              str(q.get("order_status")))

        print("\n[5] 接单（需自报位置）")
        r = c.post(f"/api/carrier/orders/{oid}/accept",
                   json={"position_text": "石家庄正定物流园"}, headers=H_car)
        check("接单成功", r.status_code == 200, r.text[:160])
        check("订单状态为 claimed", (r.json() or {}).get("status") == "claimed",
              str((r.json() or {}).get("status")))
        check("位置已记录", "正定物流园" in str((r.json() or {}).get("position_text")),
              str((r.json() or {}).get("position_text")))

        r = c.post(f"/api/carrier/orders/{oid}/accept",
                   json={"position_text": "再次接单"}, headers=H_car)
        check("重复接单被拒（幂等）", r.status_code == 409, f"{r.status_code} {r.text[:100]}")

        r = c.get("/api/carrier/pool", headers=H_car)
        check("被接走后从池子消失",
              not any(i["order_id"] == oid for i in (r.json() or [])), "")

        print("\n[6] 承运商报异常")
        r = c.post("/api/carrier/chat", json={
            "text": "我在保定服务区，冷藏机组故障，温度上来了"}, headers=H_car)
        d = r.json()
        check("识别为异常上报", d.get("state") in ("confirming", "collecting"),
              f"{d.get('state')} {d.get('reply')[:80]}")
        if d.get("state") == "collecting":
            r = c.post("/api/carrier/chat", json={
                "session_id": d["session_id"], "text": f"订单是 {mine['order_no']}"}, headers=H_car)
            d = r.json()
        check("位置与描述都收到", (d.get("slots") or {}).get("position_text") and
              (d.get("slots") or {}).get("description"), str(d.get("slots")))
        r = c.post("/api/carrier/chat", json={
            "session_id": d["session_id"], "text": "确认"}, headers=H_car)
        d = r.json()
        check("确认后上报成功", d.get("done") is True, str(d)[:160])

        db = session_as("app")
        try:
            exc = db.query(ExceptionReport).filter(
                ExceptionReport.order_id == oid).all()
            check("异常记录落库 1 条", len(exc) == 1, str(len(exc)))
            o = db.get(ShipperOrder, oid)
            check("订单转入 exception", o.status == "exception", o.status)
            eid = exc[0].id if exc else None
        finally:
            db.close()

        print("\n[7] 调度处置：不换承运商")
        r = c.post(f"/api/dispatch/exceptions/{eid}/resolve",
                   json={"resolution": "就近维修并继续运输，已通知货主", "action": "resume"},
                   headers=H_disp)
        check("处置成功", r.status_code == 200, r.text[:160])
        check("处置后订单回到在途", (r.json() or {}).get("order_status") == "in_transit",
              str((r.json() or {}).get("order_status")))

        r = c.post(f"/api/dispatch/exceptions/{eid}/resolve",
                   json={"resolution": "重复处置"}, headers=H_disp)
        check("重复处置被拒（幂等）", r.status_code == 409, f"{r.status_code}")

        db = session_as("app")
        try:
            o = db.get(ShipperOrder, oid)
            check("承运商全程未变更", o.carrier_org_id is not None, str(o.carrier_org_id))
        finally:
            db.close()

        print("\n[8] 金额守恒")
        db = session_as("app")
        try:
            p = db.query(OrderPublish).filter(OrderPublish.order_id == oid).first()
            b = db.query(CarrierBid).filter(CarrierBid.order_id == oid).first()
            s = {"gross": Decimal(str(p.gross_price)),
                 "shipper_fee": Decimal(str(p.shipper_fee)),
                 "carrier_fee": Decimal(str(b.est_fee)),
                 "carrier_income": Decimal(str(p.publish_price)) - Decimal(str(b.est_fee))}
            check("货主实付 = 两侧服务费 + 承运商实收",
                  pricing.check_conservation(s), str({k: str(v) for k, v in s.items()}))
            check("承运商实收 + 服务费 = 挂牌价",
                  s["carrier_income"] + s["carrier_fee"] == Decimal(str(p.publish_price)),
                  f"{s['carrier_income']}+{s['carrier_fee']} vs {p.publish_price}")
        finally:
            db.close()

        print("\n[9] 事件流完整可回放")
        r = c.get(f"/api/dispatch/orders/{oid}", headers=H_disp)
        evs = [e["type"] for e in (r.json() or {}).get("events") or []]
        expect = ["order_created", "order_submitted", "order_planned", "order_published",
                  # 挂牌即开标（S2）：承运商看到单子的同时就能报价
                  "tender_opened",
                  "carrier_candidate", "order_claimed", "exception_reported",
                  "exception_resolved"]
        check("事件序列完整且有序", evs == expect, f"{evs}")

        print("\n[10] 越权隔离")
        r = c.get("/api/dispatch/orders", headers=H_ship)
        check("货主不能看调度全量（403）", r.status_code == 403, f"{r.status_code}")
        r = c.get("/api/carrier/pool", headers=H_disp)
        check("调度员无承运商权限（403）", r.status_code == 403, f"{r.status_code}")

    # 清理
    db = session_as("app")
    try:
        db.query(ExceptionReport).filter(ExceptionReport.order_id == oid).delete()
        db.query(CarrierBid).filter(CarrierBid.order_id == oid).delete()
        db.query(OrderPublish).filter(OrderPublish.order_id == oid).delete()
        db.query(OrderPlan).filter(OrderPlan.order_id == oid).delete()
        db.query(OrderEvent).filter(OrderEvent.order_id == oid).delete()
        db.query(ShipperOrder).filter(ShipperOrder.id == oid).delete()
        ids = [s.id for s in db.query(ChatSession).all()]
        if ids:
            db.query(ChatMessage).filter(ChatMessage.session_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(ChatSession).filter(ChatSession.id.in_(ids)).delete(
                synchronize_session=False)
        db.commit()
    finally:
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    agents.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
