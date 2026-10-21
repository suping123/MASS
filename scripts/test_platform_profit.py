"""空驶成本与路径经停验证。

    ./.venv/Scripts/python.exe scripts/test_platform_profit.py

要点：
  1) 收益 = 运费 − 服务费 − 干线成本 − 空驶成本，四项都对得上；
  2) 空驶里程来自"承运商当前位置 → 订单起运地"，位置解析不到时**按 0 计并标注原因**，
     不猜位置；
  3) 路径精确到县级：石家庄→南京 应出现多个县级途经点。
"""
from __future__ import annotations

import atexit
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
from sca.platform.services import pricing, routing  # noqa: E402

FAILS: list[str] = []
PASSED = 0
CENT = Decimal("0.01")


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

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()
    created: list[int] = []
    # 「改过没有」和「原值是多少」要分开记：原值本身可能就是 NULL，
    # 用一个 `old_pos = None` 同时表达"还没取"和"原来是空"，就分不清该不该写回。
    state = {"touched": False, "old_pos": None, "done": False}

    def restore() -> None:
        """还原承运商位置与本次造的数据。

        这个套件会**临时改建全局档案**（``carrier_profile.current_position``），
        而断言中途抛异常（比如报价接口返回的不是预期结构、``q["quoted_price"]`` KeyError）
        会直接跳过结尾的还原代码 —— 于是位置就留在"保定市莲池区"，
        下一个套件 ``test_platform_flow`` 的「运输成本 = 里程 × 单价」就会莫名红一片：
        它拿 ``distance_km × cost_per_km``（干线）去比 ``est_cost``（干线 + 空驶），
        两者只在空驶为 0 时相等。

        所以这里注册到 ``atexit``：正常走完会调一次，异常退出也会被调一次。
        """
        if state["done"]:
            return
        state["done"] = True
        db = session_as("app")
        try:
            p = db.query(CarrierProfile).first()
            if p is not None and state["touched"]:
                # 原值是 NULL 也要写回 NULL —— 漏掉这一步，档案就从"没填位置"
                # 变成"位置在保定"，后续所有空驶计算都跟着错
                p.current_position = state["old_pos"]
            for oid in created:
                for model in (ExceptionReport, CarrierBid, OrderPublish, OrderPlan, OrderEvent):
                    db.query(model).filter(model.order_id == oid).delete()
                db.query(ShipperOrder).filter(ShipperOrder.id == oid).delete()
            for s in db.query(ChatSession).all():
                db.query(ChatMessage).filter(ChatMessage.session_id == s.id).delete()
                db.delete(s)
            db.commit()
        finally:
            db.close()

    atexit.register(restore)

    print("\n[1] 纯函数：城市归一 / 空驶 / 途经点")
    check("带行政后缀可归一", routing.resolve_city("张家港市") == "张家港",
          str(routing.resolve_city("张家港市")))
    check("自由文本可提取城市", routing.city_in_text("石家庄正定物流园") == "石家庄",
          str(routing.city_in_text("石家庄正定物流园")))
    check("服务区文本可提取", routing.city_in_text("保定服务区") == "保定",
          str(routing.city_in_text("保定服务区")))

    dh = routing.deadhead("河北省保定市", "石家庄")
    check("空驶里程非零且合理", dh.get("ok") and 100 < dh.get("deadhead_km", 0) < 260,
          str(dh))
    dh0 = routing.deadhead("石家庄正定物流园", "石家庄")
    check("已在起运地时空驶为 0", dh0.get("ok") and dh0.get("deadhead_km") == 0.0,
          str(dh0))
    bad = routing.deadhead("火星基地", "石家庄")
    check("位置无法识别时如实报错（不猜）", bad.get("ok") is False and bad.get("reason"),
          str(bad))

    r = routing.estimate_route("石家庄", "南京")
    wps = r.get("waypoints") or []
    check("路线带途经点", len(wps) >= 4, str(wps))
    check("途经点不是省会级泛化（含县级）",
          any(x not in ("石家庄", "南京", "济南", "合肥") for x in wps), str(wps))
    print(f"        石家庄→南京 {r['distance_km']} km，途经 {r['hub_count']} 站："
          + " / ".join(wps[1:-1]))

    print("\n[2] 计价公式：四段成本对得上")
    prof = pricing.carrier_profit(Decimal("11760"), Decimal("986.8"),
                                  Decimal("2.6"), Decimal("0.02"), Decimal("160.1"))
    fee, lh, dhc = prof["carrier_fee"], prof["linehaul_cost"], prof["deadhead_cost"]
    check("服务费 = 运费 × 2%", fee == (Decimal("11760") * Decimal("0.02")).quantize(CENT), str(fee))
    check("干线成本 = 订单里程 × 单价",
          lh == (Decimal("986.8") * Decimal("2.6")).quantize(CENT), str(lh))
    check("空驶成本 = 空驶里程 × 单价",
          dhc == (Decimal("160.1") * Decimal("2.6")).quantize(CENT), str(dhc))
    check("总成本 = 干线 + 空驶", prof["est_cost"] == lh + dhc, str(prof["est_cost"]))
    check("收益 = 运费 − 服务费 − 总成本",
          prof["profit"] == Decimal("11760") - fee - lh - dhc, str(prof["profit"]))
    print(f"        11760 − {fee} − {lh} − {dhc} = {prof['profit']}")

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}

        db = session_as("app")
        try:
            p = db.query(CarrierProfile).first()
            state["old_pos"] = p.current_position
            state["touched"] = True                  # 先记"改过了"，再落库
            p.current_position = "保定市莲池区"      # 故意设在离起运地 160km 的地方
            db.commit()
        finally:
            db.close()

        print("\n[3] 端到端：空驶进收益、进数据库")
        r = c.post("/api/shipper/chat",
                   json={"text": "石家庄到南京 冷链 8吨 明天下午6点前 预算1.2万"},
                   headers=H_ship)
        sid = (r.json() or {})["session_id"]
        r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H_ship)
        oid = (r.json() or {}).get("order_id")
        created.append(oid)
        r = c.post(f"/api/dispatch/orders/{oid}/plan", headers=H_disp)
        check("调度方案带途经点", (r.json() or {}).get("plan", {}).get("provider") == "offline-estimate",
              str(r.json())[:160])
        c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)

        r = c.post(f"/api/carrier/orders/{oid}/quote", headers=H_car)
        q = r.json() or {}
        check("试算成功", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        check("返回空驶里程（非零）", (q.get("deadhead_km") or 0) > 50, str(q.get("deadhead_km")))
        check("返回空驶成本", (q.get("deadhead_cost") or 0) > 0, str(q.get("deadhead_cost")))
        check("空驶来源记录为当前位置", "保定" in str(q.get("deadhead_from")),
              str(q.get("deadhead_from")))
        expect = (Decimal(str(q["quoted_price"])) - Decimal(str(q["carrier_fee"]))
                  - Decimal(str(q["linehaul_cost"])) - Decimal(str(q["deadhead_cost"])))
        check("收益与四项成本自洽", abs(float(expect) - q["profit"]) < 0.02,
              f"期望 {expect} 实际 {q['profit']}")
        print(f"        运费 {q['quoted_price']:,} − 服务费 {q['carrier_fee']:,} "
              f"− 干线 {q['linehaul_cost']:,} − 空驶 {q['deadhead_cost']:,} "
              f"= 收益 {q['profit']:,}（空驶 {q['deadhead_km']} km）")

        db = session_as("app")
        try:
            b = db.query(CarrierBid).filter(CarrierBid.order_id == oid).first()
            check("空驶里程落库", b.deadhead_km is not None and float(b.deadhead_km) > 50,
                  str(b.deadhead_km))
            check("空驶成本落库", b.deadhead_cost is not None and float(b.deadhead_cost) > 0,
                  str(b.deadhead_cost))
            check("干线成本落库", b.linehaul_cost is not None, str(b.linehaul_cost))
            check("位置快照落库", "保定" in (b.deadhead_from or ""), str(b.deadhead_from))
        finally:
            db.close()

        print("\n[4] 公共池快算也扣空驶")
        r = c.get("/api/carrier/pool", headers=H_car)
        item = next((i for i in (r.json() or []) if i["order_id"] == oid), None)
        # 试算只写 carrier_bid，不推进订单状态（S2 起），订单仍是 published，当然还在池中
        if item:
            check("池子返回空驶字段", "deadhead_km" in item, str(item)[:200])
            check("池子收益已扣空驶",
                  item.get("quick_profit") is not None and
                  item["quick_profit"] < item["publish_price"], str(item.get("quick_profit")))
        else:
            check("池子（该单已进入候选仍可见）", False, "未找到该单")

        print("\n[5] 位置无法识别时如实标注")
        db = session_as("app")
        try:
            p = db.query(CarrierProfile).first()
            p.current_position = "不知道在哪里"
            db.commit()
        finally:
            db.close()
        r = c.post(f"/api/carrier/orders/{oid}/quote", headers=H_car)
        q2 = r.json() or {}
        check("空驶按 0 计", q2.get("deadhead_km") == 0, str(q2.get("deadhead_km")))
        check("给出未计原因（不静默）", bool(q2.get("deadhead_note")), str(q2.get("deadhead_note")))
        print(f"        提示：{q2.get('deadhead_note')}")

    # 还原承运商位置与本次造的数据（走 finish()，与 atexit 里那次是同一个函数）
    restore()
    agents.stop()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
