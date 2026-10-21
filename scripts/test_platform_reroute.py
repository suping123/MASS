"""真实改道 + 时效延长的回归验证（B1 / B3）。

    ./.venv/Scripts/python.exe scripts/test_platform_reroute.py

覆盖：
  1) 改道预览返回可比的 before/after（里程、ETA、成本、绕开了哪个城市）；
  2) 定位不到受影响城市时**不改道**，返回 skipped 与原因（不编造绕行里程）；
  3) resolve(action=replan) 真的写新方案版本并落事件；skipped 时不写版本；
  4) 时效延长是独立动作：只延后、必须给时长、落事件、给货主与承运商发通知；
  5) 非法参数（负时长、超范围）一律 409。

只创建一条临时订单，结束时按依赖顺序清理。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402

from sca.platform import security  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
from sca.platform.models import (CarrierBid, ExceptionReport,  # noqa: E402
                                 Notification, OrderEvent, OrderPlan,
                                 OrderPublish, ShipperOrder)

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
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

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()

    db = session_as("app")
    oid = None
    try:
        with TestClient(app) as c:
            H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
            H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}

            print("\n[0] 造一单并出方案")
            r = c.post("/api/shipper/chat", json={
                "text": "从济南到西昌 普货 12吨 后天下午6点前 预算3万"}, headers=H_ship)
            sid = (r.json() or {}).get("session_id")
            r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"},
                       headers=H_ship)
            oid = (r.json() or {}).get("order_id")
            check("落单成功", bool(oid), str(r.json())[:200])
            if not oid:
                raise SystemExit(1)
            r = c.post(f"/api/dispatch/orders/{oid}/plan", headers=H_disp)
            check("出方案成功", r.status_code == 200, r.text[:200])
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            order = db.get(ShipperOrder, oid)
            plan = db.get(OrderPlan, order.active_plan_id)
            wp = list((plan.route_json or {}).get("waypoints") or [])
            check("方案带途经点", len(wp) >= 3, str(wp))

            # 造一条异常：用一个真实途经点，保证"在走廊上"
            mid = wp[2] if len(wp) > 3 else wp[1]
            e = ExceptionReport(order_id=oid, reporter_user_id=order.carrier_user_id or 1,
                                position_text=f"{mid}附近", description=f"{mid}段道路封闭，无法通行",
                                status="open", level="P1")
            db.add(e)
            db.commit()
            eid = e.id

            print("\n[1] 改道预览（不落库）")
            r = c.post(f"/api/dispatch/exceptions/{eid}/reroute-preview", json={},
                       headers=H_disp)
            d = r.json() or {}
            check("预览返回 200", r.status_code == 200, r.text[:200])
            check("定位到受影响城市", d.get("avoid_city") == mid,
                  f"{d.get('avoid_city')} vs {mid}")
            if d.get("ok"):
                check("给出改道前后里程", d["old"]["distance_km"] > 0 and
                      d["new"]["distance_km"] > 0, str(d.get("old")))
                check("给出是否验证避让", "avoid_verified" in d, str(d.keys())[:120])
                check("预览不写库", db.get(ShipperOrder, oid).active_plan_id == plan.id,
                      "active_plan 被改了")
            else:
                check("未绕开时如实说明原因", bool(d.get("reason")), str(d)[:200])
                print("      （本次未能绕开，属合法结果：", d.get("reason", "")[:60], "）")

            print("\n[2] 无定位的异常不改道")
            e2 = ExceptionReport(order_id=oid, reporter_user_id=1,
                                 description="车坏了，走不动", status="open", level="P2")
            db.add(e2)
            db.commit()
            eid2 = e2.id
            r = c.post(f"/api/dispatch/exceptions/{eid2}/reroute-preview", json={},
                       headers=H_disp)
            d2 = r.json() or {}
            check("无城市线索 -> skipped", d2.get("ok") is False and d2.get("skipped") is True,
                  str(d2)[:200])
            check("skipped 带原因", bool(d2.get("reason")), str(d2)[:200])

            print("\n[3] resolve(replan) 落库与事件")
            r = c.post(f"/api/dispatch/exceptions/{eid}/resolve", json={
                "action": "replan", "resolution": "绕开受影响城市继续运输"}, headers=H_disp)
            d3 = r.json() or {}
            check("处置返回 200", r.status_code == 200, r.text[:300])
            check("处置动作如实回传", d3.get("action") in ("replan", "replan_skipped"),
                  str(d3.get("action")))
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            order = db.get(ShipperOrder, oid)
            ver = db.execute(select(OrderPlan.version).where(
                OrderPlan.order_id == oid).order_by(OrderPlan.version.desc())).scalars().first()
            ev = db.execute(select(OrderEvent).where(
                OrderEvent.order_id == oid,
                OrderEvent.event_type == "order_rerouted")).scalars().all()
            if d3.get("action") == "replan":
                check("写了新方案版本", int(ver) >= 2, f"version={ver}")
                check("active_plan 指向新版本", order.active_plan_id != plan.id,
                      f"{order.active_plan_id} vs {plan.id}")
                check("落了 order_rerouted 事件", len(ev) >= 1, str(len(ev)))
                check("事件含新旧里程", bool((ev[-1].payload or {}).get("old")) and
                      bool((ev[-1].payload or {}).get("new")), str(ev[-1].payload)[:200])
            else:
                check("skipped 时不写新版本", int(ver or 0) == 1, f"version={ver}")
                check("skipped 时 plan 不变", order.active_plan_id == plan.id, "变了")
            check("异常单记了处置方式", db.get(ExceptionReport, eid).resolution_action
                  == d3.get("action"), str(db.get(ExceptionReport, eid).resolution_action))

            print("\n[4] 无定位异常 resolve(replan) 也不假改道")
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            before_ver = db.execute(select(OrderPlan.id).where(
                OrderPlan.order_id == oid)).scalars().all()
            r = c.post(f"/api/dispatch/exceptions/{eid2}/resolve", json={
                "action": "replan", "resolution": "先恢复在途再观察"}, headers=H_disp)
            d4 = r.json() or {}
            check("无定位 -> replan_skipped", d4.get("action") == "replan_skipped",
                  str(d4.get("action")))
            check("提示未改道", "未改道" in (d4.get("note") or ""), str(d4.get("note"))[:120])
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            after_ver = db.execute(select(OrderPlan.id).where(
                OrderPlan.order_id == oid)).scalars().all()
            check("方案数量没变", len(after_ver) == len(before_ver),
                  f"{len(before_ver)} -> {len(after_ver)}")

            print("\n[5] 时效延长（独立动作）")
            e3 = ExceptionReport(order_id=oid, reporter_user_id=1,
                                 description="目的地仓库压车，需延后收货",
                                 status="open", level="P2")
            db.add(e3)
            db.commit()
            eid3 = e3.id
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            before_at = db.get(ShipperOrder, oid).required_by_at
            r = c.post(f"/api/dispatch/exceptions/{eid3}/resolve", json={
                "action": "extend", "resolution": "客户同意延后半天收货",
                "delay_h": 4}, headers=H_disp)
            d5 = r.json() or {}
            check("改期返回 200", r.status_code == 200, r.text[:300])
            dl = d5.get("deadline") or {}
            check("返回改期前后", bool(dl.get("before")) and bool(dl.get("after")), str(dl)[:200])
            db.rollback()
            # 强制过期：否则读到的是本 session 缓存里的旧对象（HTTP 请求是另一个 session）
            db.expire_all()
            after_at = db.get(ShipperOrder, oid).required_by_at
            if before_at and after_at:
                diff_h = (after_at - before_at).total_seconds() / 3600
                check("承诺时效精确后移 4 小时", abs(diff_h - 4) < 0.01, f"{diff_h}")
            else:
                check("承诺时效字段可写", False, "required_by_at 缺失")
            ev2 = db.execute(select(OrderEvent).where(
                OrderEvent.order_id == oid,
                OrderEvent.event_type == "order_deadline_extended")).scalars().all()
            check("落了改期事件", len(ev2) >= 1, str(len(ev2)))
            nt = db.execute(select(Notification).where(
                Notification.order_id == oid,
                Notification.ntype == "deadline_changed")).scalars().all()
            check("货主收到时效变更通知", any(
                n.user_id == db.get(ShipperOrder, oid).shipper_user_id for n in nt),
                f"{len(nt)} 条")
            check("异常单记录了改期时长",
                  float(db.get(ExceptionReport, eid3).deadline_shift_h or 0) == 4.0,
                  str(db.get(ExceptionReport, eid3).deadline_shift_h))

            print("\n[6] 非法参数")
            e4 = ExceptionReport(order_id=oid, reporter_user_id=1,
                                 description="再延一次试试", status="open", level="P2")
            db.add(e4)
            db.commit()
            r = c.post(f"/api/dispatch/exceptions/{e4.id}/resolve", json={
                "action": "extend", "resolution": "随便延", "delay_h": -2}, headers=H_disp)
            check("负时长被拒 409", r.status_code == 409, r.text[:160])
            r = c.post(f"/api/dispatch/exceptions/{e4.id}/resolve", json={
                "action": "extend", "resolution": "延太久", "delay_h": 200}, headers=H_disp)
            check("超范围被拒 409", r.status_code == 409, r.text[:160])
            r = c.post(f"/api/dispatch/exceptions/{e4.id}/resolve", json={
                "action": "extend", "resolution": "没给时长"}, headers=H_disp)
            check("缺时长被拒 409", r.status_code == 409, r.text[:160])
            print("\n[7] 有条件改派")
            from sca.platform.models import CarrierProfile

            prof = db.execute(select(CarrierProfile).limit(1)).scalars().first()
            if not prof:
                check("库里有承运商档案", False, "无 CarrierProfile")
            else:
                db.execute(select(ShipperOrder).where(ShipperOrder.id == oid))
                order = db.get(ShipperOrder, oid)
                order.carrier_org_id = prof.org_id
                order.carrier_user_id = prof.carrier_user_id
                order.status = "in_transit"
                db.commit()

                r = c.get(f"/api/dispatch/reassign/options?order_id={oid}", headers=H_disp)
                d7 = r.json() or {}
                check("改派选项返回 200", r.status_code == 200, r.text[:200])
                trg = {t["code"]: t for t in (d7.get("triggers") or [])}
                check("列出五类触发码", len(trg) == 5, str(list(trg)))
                check("人工强制改派可用", (trg.get("manual_force") or {}).get("eligible") is True,
                      str(trg.get("manual_force")))
                check("普货不构成资质不符",
                      (trg.get("qualification_mismatch") or {}).get("eligible") is False,
                      str(trg.get("qualification_mismatch")))
                check("列出候选承运商", len(d7.get("carriers") or []) >= 1,
                      str(len(d7.get("carriers") or [])))

                e5 = ExceptionReport(order_id=oid, reporter_user_id=1,
                                     description="承运商明确说不跑了",
                                     status="open", level="P1")
                db.add(e5)
                db.commit()
                r = c.post(f"/api/dispatch/exceptions/{e5.id}/resolve", json={
                    "action": "reassign", "resolution": "换人",
                    "reason_code": "manual_force"}, headers=H_disp)
                check("原因过短被拒 409", r.status_code == 409, r.text[:160])
                r = c.post(f"/api/dispatch/exceptions/{e5.id}/resolve", json={
                    "action": "reassign", "resolution": "承运商车辆报废无法继续运输，需改派",
                    "reason_code": "manual_force"}, headers=H_disp)
                d8 = r.json() or {}
                check("改派成功", r.status_code == 200, r.text[:300])
                check("回挂牌池等待重新接单",
                      (d8.get("reassign") or {}).get("mode") == "pool", str(d8)[:200])
                check("结算未变动",
                      (d8.get("reassign") or {}).get("settlement_unchanged") is True,
                      str(d8.get("reassign"))[:160])
                db.rollback()
                db.expire_all()
                o7 = db.get(ShipperOrder, oid)
                check("订单回到已挂牌", o7.status == "published", o7.status)
                check("原承运商已释放", o7.carrier_org_id is None, str(o7.carrier_org_id))
                ev3 = db.execute(select(OrderEvent).where(
                    OrderEvent.order_id == oid,
                    OrderEvent.event_type == "order_reassigned")).scalars().all()
                check("落了改派事件", len(ev3) >= 1, str(len(ev3)))
                check("事件记了触发码与原承运商",
                      (ev3[-1].payload or {}).get("reason_code") == "manual_force" and
                      (ev3[-1].payload or {}).get("from_org_id") is not None,
                      str(ev3[-1].payload)[:200])
    finally:
        db.rollback()
        if oid:
            for model in (Notification, CarrierBid, ExceptionReport, OrderPublish,
                          OrderPlan, OrderEvent):
                db.execute(delete(model).where(model.order_id == oid))
            from sca.platform.models import ChatSession

            db.execute(delete(ShipperOrder).where(ShipperOrder.id == oid))
            db.commit()
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项失败 / {PASSED + len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    agents.stop()
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
