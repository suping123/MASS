"""招标规则的纯函数验证（S0）。

    ./.venv/Scripts/python.exe scripts/test_tender.py --stage rules

S0 只测规则本身，不连数据库、不建订单。理由：裁决逻辑一旦混进 Session，
边界条件（零投标 / 超预算 / 全部资质不符）就只能靠造真实订单来测，
而造订单又会引入状态机噪声。纯函数先在假数据上把边界测干净，
S2 接线时只需要验证"数据取得对、结果落得对"。

覆盖：
  1) 承运商报价：成本加成、低于成本底线抬价、超预算顶格、成本高于预算
  2) 硬过滤：预算 / 时效 / 载重 / 温控 / 危品 / 空驶，原因码要稳定
  3) 打分：价格主因子、时效相对基准、权重归一化
  4) 裁决：综合分不是最低价 / 零投标 / 延期后流标 / 超预算两档 / 全淘汰
  5) 协商：接受 / 末轮拒绝 / 已到底线 / 正常让步
  6) 货主提价建议：上限内 / 超上限
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.platform.services import tender as T  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def ok(cond, name: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


REQ = {
    "budget": Decimal("5000"),
    "required_eta_h": Decimal("30"),
    "weight_t": Decimal("10"),
    "need_temp": False,
    "need_haz": False,
    "deadhead_max_km": Decimal("800"),
}


def make_bid(org: int = 1, quote: str = "4800", eta: str = "24",
             rating: str = "4.5", deadhead: str = "100",
             capacity: str = "30", temp_ctrl: bool = True,
             hazmat: bool = True) -> dict:
    return {"carrier_org_id": org, "quote": Decimal(quote), "eta_h": Decimal(eta),
            "rating": Decimal(rating), "deadhead_km": Decimal(deadhead),
            "capacity_t": Decimal(capacity), "temp_ctrl": temp_ctrl,
            "hazmat": hazmat}


# ---------------------------------------------------------------------------
def check_quote() -> None:
    print("\n[报价：承运商该报多少]")
    r = T.carrier_quote(Decimal("1000"))
    ok(r["quote"] == Decimal("1080.00"), "成本 1000 × (1+8%) = 1080", str(r["quote"]))

    r = T.carrier_quote(Decimal("1000"), target_profit_rate=Decimal("0.01"))
    ok(r["profit_rate"] == Decimal("0.05"),
       "目标利润率低于成本底线时抬到 5%", str(r["profit_rate"]))
    ok(r["quote"] == Decimal("1050.00"), "抬价后报价 = 1050", str(r["quote"]))

    r = T.carrier_quote(Decimal("4900"), budget=Decimal("5000"))
    ok(r["capped"] is True and r["quote"] == Decimal("5000.00"),
       "成本加成超预算时顶格报预算", f"quote={r['quote']} capped={r['capped']}")

    r = T.carrier_quote(Decimal("6000"), budget=Decimal("5000"))
    ok(r["below_cost"] is True,
       "成本本身高于预算时标记 below_cost（应弃标）", str(r))

    r = T.carrier_quote(Decimal("1000"))
    ok(r["floor"] == Decimal("1050.00"), "成本底线 = 成本 × 1.05", str(r["floor"]))


# ---------------------------------------------------------------------------
def check_filter() -> None:
    print("\n[硬过滤：不合格的投标不进打分]")
    cases = [
        ("over_budget", make_bid(quote="5200"), REQ),
        ("eta_miss", make_bid(eta="32"), REQ),
        ("capacity", make_bid(capacity="8"), REQ),
        ("temp", make_bid(temp_ctrl=False), {**REQ, "need_temp": True}),
        ("hazmat", make_bid(hazmat=False), {**REQ, "need_haz": True}),
        ("deadhead", make_bid(deadhead="900"), REQ),
    ]
    for expect_code, b, req in cases:
        passed, code, msg = T.hard_filter(b, req)
        ok(passed is False and code == expect_code,
           f"淘汰原因码 = {expect_code}", f"got {code} / {msg}")
        ok(bool(msg), f"{expect_code} 必须给人话说明", msg)

    passed, code, msg = T.hard_filter(make_bid(), REQ)
    ok(passed is True and code == "", "全部满足时合规且无原因码", f"{code} {msg}")

    # 超预算的说明要带百分比，否则调度看不懂"差多少"
    _, _, msg = T.hard_filter(make_bid(quote="5200"), REQ)
    ok("4.00%" in msg, "超预算说明包含超出比例", msg)


# ---------------------------------------------------------------------------
def check_score() -> None:
    print("\n[打分：四个因子都要起作用]")
    cheap = make_bid(org=1, quote="4000")
    pricey = make_bid(org=2, quote="4800")
    s1, f1 = T.score_bid(cheap, REQ)
    s2, f2 = T.score_bid(pricey, REQ)
    ok(s1 > s2, "其他条件相同时报价低者得分高", f"{s1} vs {s2}")

    fast = make_bid(eta="20")
    slow = make_bid(eta="24")
    _, ff = T.score_bid(fast, REQ, fastest_eta=Decimal("20"))
    _, fs = T.score_bid(slow, REQ, fastest_eta=Decimal("20"))
    ok(ff["eta"] == 1.0, "本批次最快承诺的时效因子 = 1", str(ff["eta"]))
    ok(fs["eta"] < 1.0,
       "同样满足时限但更慢的，时效因子要低（否则 0.2 权重白给）", str(fs["eta"]))

    _, f_no_eta = T.score_bid(make_bid(), {**REQ, "required_eta_h": None})
    ok(f_no_eta["eta"] == 0.5, "没有时限要求时时效因子给中性 0.5", str(f_no_eta["eta"]))

    only_price = {"price": 4.0, "eta": 0.0, "rating": 0.0, "deadhead": 0.0}
    s, f = T.score_bid(pricey, REQ, only_price)
    ok(abs(s - f["price"]) < 1e-6,
       "权重合计不为 1 时归一化，分数仍可比较", f"score={s} f_price={f['price']}")


# ---------------------------------------------------------------------------
def check_decide() -> None:
    print("\n[裁决：选的是综合分，不是最低价]")
    cheap_weak = make_bid(org=1, quote="4000", rating="2.0", eta="30", deadhead="700")
    pricey_strong = make_bid(org=2, quote="4800", rating="5.0", eta="24", deadhead="50")
    r = T.decide([cheap_weak, pricey_strong], REQ)
    ok(r["status"] == "awarded", "有合规投标应裁决成功", r["status"])
    ok(r["winner"]["carrier_org_id"] == 2,
       "赢家是综合分最高的（不是最便宜的）", f"winner={r['winner']['carrier_org_id']}")
    ok(all("verdict" in c for c in r["candidates"]),
       "每家投标都要有裁决结论，可回放", str([c["verdict"] for c in r["candidates"]]))

    r = T.decide([], REQ)
    ok(r["status"] == "extended", "零投标且未延期 → 建议延期", r["status"])
    r = T.decide([], REQ, extended=True)
    ok(r["status"] == "failed", "延期后仍零投标 → 流标", r["status"])

    r = T.decide([make_bid(quote="5300")], REQ)
    ok(r["status"] == "negotiating", "超预算 6% → 进入协商", r["status"])
    ok(r["target"]["carrier_org_id"] == 1, "协商对象是最低报价那家", str(r.get("target")))

    r = T.decide([make_bid(quote="6000")], REQ)
    ok(r["status"] == "failed", "超预算 20% → 直接流标", r["status"])

    r = T.decide([make_bid(capacity="5"), make_bid(org=2, temp_ctrl=False)],
                 {**REQ, "need_temp": True})
    ok(r["status"] == "failed", "全部被硬过滤淘汰 → 流标", r["status"])
    ok("载重" in r["reason"] or "温控" in r["reason"],
       "流标原因要说清是哪一项不合格", r["reason"])

    # 完全相同的报价与评分：靠承运商 id 稳定排序，不能让 dict 顺序决定谁中标
    a = make_bid(org=9)
    b = make_bid(org=3)
    r = T.decide([a, b], REQ)
    ok(r["winner"]["carrier_org_id"] == 3,
       "平分时按承运商 id 稳定排序", str(r["winner"]["carrier_org_id"]))

    # 资质不符的低价不该触发协商：便宜不是因为能谈，是因为它干不了
    r = T.decide([make_bid(quote="3000", capacity="2")], REQ)
    ok(r["status"] == "failed",
       "因资质淘汰的低价投标不进入协商", r["status"])


# ---------------------------------------------------------------------------
def check_negotiation() -> None:
    print("\n[协商：双方底线]")
    r = T.carrier_reply(counter_amount=Decimal("1200"), floor_price=Decimal("1100"),
                        own_quote=Decimal("1500"), round_no=1)
    ok(r["action"] == "accept", "还价高于成本底线 → 接受", r["action"])

    r = T.carrier_reply(counter_amount=Decimal("900"), floor_price=Decimal("1100"),
                        own_quote=Decimal("1500"), round_no=3)
    ok(r["action"] == "reject", "末轮仍低于底线 → 拒绝", r["action"])

    r = T.carrier_reply(counter_amount=Decimal("900"), floor_price=Decimal("1100"),
                        own_quote=Decimal("1100"), round_no=1)
    ok(r["action"] == "reject", "上轮已报到成本线 → 无法再让", r["action"])

    r = T.carrier_reply(counter_amount=Decimal("900"), floor_price=Decimal("1100"),
                        own_quote=Decimal("1500"), round_no=2)
    ok(r["action"] == "counter" and r["amount"] == Decimal("1100.00"),
       "还有轮次 → 让到成本线", str(r))
    ok(r["amount"] < Decimal("1500"),
       "回价必须低于自己上轮报价（不让步的回价没有意义）", str(r["amount"]))


# ---------------------------------------------------------------------------
# S2：数据库接线（开标 -> 投标 -> 裁决）
# ---------------------------------------------------------------------------
ORG_A, ORG_B = 9901, 9902          # 测试专用承运商组织，跑完即删
U_DISPATCH = {"user_id": 2, "account": "dispatcher1", "role": "dispatcher", "org_id": 1}
U_A = {"user_id": 3, "account": "carrier1", "role": "carrier", "org_id": ORG_A}
U_B = {"user_id": 3, "account": "carrier1", "role": "carrier", "org_id": ORG_B}
CENT = Decimal("0.01")


def _cleanup(db) -> None:
    from sqlalchemy import delete

    from sca.platform.models import (AgentMessage, CarrierBid, CarrierProfile,
                                     Notification, OrderEvent, OrderPlan,
                                     OrderPublish, ShipperOrder, SysOrg, Tender)

    ids = [o.id for o in db.execute(
        ShipperOrder.__table__.select().where(
            ShipperOrder.order_no.like("SOTEST%"))).all()]
    for oid in ids:
        for model in (Tender, CarrierBid, OrderEvent):
            db.execute(delete(model).where(model.order_id == oid))
        # 协商消息与站内通知没有外键，必须按 thread/order 显式清，
        # 否则重复跑测试会在总线里留下上一轮的 counter，被当成新消息处理
        db.execute(delete(AgentMessage).where(
            AgentMessage.thread_id == f"order:{oid}"))
        db.execute(delete(Notification).where(Notification.order_id == oid))
        db.execute(delete(OrderPublish).where(OrderPublish.order_id == oid))
        db.execute(delete(OrderPlan).where(OrderPlan.order_id == oid))
        db.execute(delete(ShipperOrder).where(ShipperOrder.id == oid))
    db.execute(delete(CarrierProfile).where(CarrierProfile.org_id.in_((ORG_A, ORG_B))))
    db.execute(delete(SysOrg).where(SysOrg.id.in_((ORG_A, ORG_B))))
    db.commit()


def _mk_carrier(db, org_id: int, name: str, *, rate: str, pos: str,
                rating: str, capacity: str = "30") -> None:
    from sca.platform.models import CarrierProfile, SysOrg

    db.add(SysOrg(id=org_id, name=name, org_type="carrier"))
    # 先落 org 再落 profile：模型里没有 relationship，SQLAlchemy 排不出外键顺序
    db.flush()
    db.add(CarrierProfile(org_id=org_id, cost_per_km=Decimal(rate),
                          capacity_t=Decimal(capacity), rating=Decimal(rating),
                          current_position=pos))
    db.flush()


def _mk_order(db, no: str, *, budget: str, dist: str = "1000", eta: str = "24",
              origin: str = "石家庄", dest: str = "南京", required_in_h: int = 40):
    """造一张已挂牌的订单（计划 + 挂牌单齐备，等价于 publish_order 的产物）。"""
    from datetime import datetime, timedelta

    from sca.platform.models import OrderPlan, OrderPublish, ShipperOrder

    o = ShipperOrder(order_no=no, shipper_user_id=1, shipper_org_id=2,
                     origin=origin, dest=dest, cargo_type="普货",
                     weight_t=Decimal("10"), budget_max=Decimal(budget),
                     required_by=f"{required_in_h}小时内",
                     required_by_at=datetime.now() + timedelta(hours=required_in_h),
                     status="planned")
    db.add(o)
    db.flush()
    plan = OrderPlan(order_id=o.id, version=1, distance_km=Decimal(dist),
                     eta_h=Decimal(eta), cost_est=Decimal("2600"),
                     route_json={"provider": "test"})
    db.add(plan)
    db.flush()
    o.active_plan_id = plan.id
    gross = Decimal(budget)
    fee = (gross * Decimal("0.02")).quantize(CENT)
    pub = OrderPublish(order_id=o.id, plan_id=plan.id, gross_price=gross,
                       shipper_fee_rate=Decimal("0.02"), shipper_fee=fee,
                       carrier_fee_rate=Decimal("0.02"),
                       publish_price=gross - fee, status="open", published_by=2)
    db.add(pub)
    o.status = "published"
    db.flush()
    return o, plan, pub


def check_db() -> None:
    from datetime import datetime

    from sqlalchemy import select

    from sca.platform.db import session_as
    from sca.platform.models import CarrierBid, OrderPlan, ShipperOrder, Tender
    from sca.platform.services import carrier_ops, tender_ops

    db = session_as("app")
    try:
        _cleanup(db)
        _mk_carrier(db, ORG_A, "测试承运商甲", rate="2.60", pos="石家庄正定物流园",
                    rating="5.00")
        _mk_carrier(db, ORG_B, "测试承运商乙", rate="2.00", pos="济南",
                    rating="2.00")
        db.commit()

        # ---------------------------------------------------------------- 开标
        print("\n[DB-1] 挂牌单开标")
        oa, plan_a, pub_a = _mk_order(db, "SOTEST-A", budget="5000")
        db.commit()
        t = tender_ops.open_tender(db, oa, U_DISPATCH)
        ok(t["status"] == "open", "开标即进入收标中", str(t.get("status")))
        ok(Decimal(str(t["budget"])) == pub_a.publish_price,
           "招标上限＝挂牌价（不是货主预算）", f"{t['budget']} vs {pub_a.publish_price}")
        again = tender_ops.open_tender(db, oa, U_DISPATCH)
        ok(again["reused"] and again["tender_id"] == t["tender_id"],
           "重复开标幂等，不产生第二份标书", str(again))

        # ---------------------------------------------------------------- 投标
        print("\n[DB-2] 投标不改变订单状态（旧 bug 回归）")
        r1 = tender_ops.submit_bid(db, U_A, oa.id, note="按成本加成自动报价")
        db.refresh(oa)
        ok(oa.status == "published", "投标后订单仍是 published（没被浏览记录推进）",
           oa.status)
        floor = Decimal(str(r1["floor"]))
        expect = floor * Decimal("1.08")
        ok(abs(Decimal(str(r1["quote"])) - expect) <= CENT,
           "自动报价 = 成本底线 × 1.08", f"{r1['quote']} vs {expect}")
        ok(r1["cost_total"] > 0 and r1["linehaul_cost"] > 0,
           "成本按里程×单价算出来，不是拍脑袋", str(r1["cost_total"]))

        r1b = tender_ops.submit_bid(db, U_A, oa.id, quote_amount=4800, eta_h=24)
        ok(r1b["bid_round"] == 2, "同一家再报一次 → 轮次 +1", str(r1b["bid_round"]))
        row = db.execute(select(CarrierBid).where(
            CarrierBid.order_id == oa.id,
            CarrierBid.carrier_org_id == ORG_A)).scalars().first()
        ok(len(row.bid_history or []) == 2, "两轮报价都留在 bid_history 里",
           str(row.bid_history))
        ok(row.quoted_price == pub_a.publish_price,
           "quoted_price 仍是服务端回填口径（与 quote_amount 分开）",
           f"{row.quoted_price} vs {pub_a.publish_price}")

        r2 = tender_ops.submit_bid(db, U_B, oa.id, quote_amount=4400, eta_h=24)
        ok(r2["quote"] == 4400.0, "承运商可以自己报价（不再只是接受挂牌价）", str(r2["quote"]))

        # ---------------------------------------------------------------- 裁决
        print("\n[DB-3] 裁决：综合分最高，不是最低价")
        ok(tender_ops.close_tender(db, oa, U_DISPATCH)["closed"] is False,
           "截止时间未到且没 force → 不裁决", "")
        res = tender_ops.close_tender(db, oa, U_DISPATCH, force=True)
        ok(res["status"] == "awarded", "有合规投标 → 定标", str(res.get("status")))
        win = res.get("winner") or {}
        ok(win.get("carrier_org_id") == ORG_A,
           "中标的是综合分高的一家，不是报价最低的一家",
           f"winner={win.get('carrier_org_id')} quote={win.get('quote')} "
           f"vs B 报 4400")
        db.refresh(oa)
        ok(oa.status == "candidate", "定标后订单推进到 candidate", oa.status)
        ok(db.get(OrderPlan, plan_a.id).arbitration,
           "裁决证据写进 order_plan.arbitration（这个字段以前一直是空的）", "")
        t_row = db.get(Tender, t["tender_id"])
        ok(bool(t_row.arbitration) and t_row.winner_bid_id,
           "招标单自己留了一份完整证据与中标 bid_id", str(t_row.winner_bid_id))
        ok(len((t_row.arbitration or {}).get("candidates") or []) == 2,
           "两家报价都进了证据（含淘汰原因）", "")

        loser = db.execute(select(CarrierBid).where(
            CarrierBid.order_id == oa.id,
            CarrierBid.carrier_org_id == ORG_B)).scalars().first()
        db.refresh(loser)
        ok(loser.decision == "rejected", "落标方被显式置为 rejected", loser.decision)

        print("\n[DB-4] 定标后的边界")
        dup = tender_ops.close_tender(db, oa, U_DISPATCH, force=True)
        ok(dup.get("reused") is True, "重复裁决幂等，不重算", str(dup.get("reused")))
        try:
            # S5 起执行体叫 accept_core（HTTP/对话都只投消息，由 daemon 调它）
            carrier_ops.accept_core(db, U_B, oa.id, "济南")
            ok(False, "非中标方不能截胡", "居然接单成功了")
        except ValueError as exc:
            ok("裁决" in str(exc) or "其他承运商" in str(exc),
               "非中标方接单被拒，且说明原因", str(exc))
        won_bid = db.execute(select(CarrierBid).where(
            CarrierBid.order_id == oa.id,
            CarrierBid.carrier_org_id == ORG_A)).scalars().first()
        expect_profit = (Decimal(str(won_bid.quote_amount))
                         - Decimal(str(won_bid.quote_amount)) * Decimal("0.02")
                         - Decimal(str(won_bid.est_cost)))
        ok(abs(Decimal(str(won_bid.est_profit)) - expect_profit) <= CENT,
           "收益 = 报价 − 承运商侧服务费 − 成本（金额链没被裁决改坏）",
           f"{won_bid.est_profit} vs {expect_profit}")

        # ---------------------------------------------------------------- 协商档
        print("\n[DB-5] 超预算分两档：可谈 / 直接流标")
        ob, _, _ = _mk_order(db, "SOTEST-B", budget="5000")
        db.commit()
        tender_ops.open_tender(db, ob, U_DISPATCH)
        tender_ops.submit_bid(db, U_A, ob.id, quote_amount=5500, eta_h=24)  # +12.2%
        rb = tender_ops.close_tender(db, ob, U_DISPATCH, force=True)
        ok(rb["status"] == "negotiating", "超预算 12% → 进入协商", str(rb.get("status")))
        ok((rb.get("target") or {}).get("carrier_org_id") == ORG_A,
           "协商对象是最低报价那家", str((rb.get("target") or {}).get("carrier_org_id")))
        db.refresh(ob)
        ok(ob.status == "published", "协商阶段订单状态不变（还没定标）", ob.status)

        oc, _, _ = _mk_order(db, "SOTEST-C", budget="5000")
        db.commit()
        tender_ops.open_tender(db, oc, U_DISPATCH)
        tender_ops.submit_bid(db, U_A, oc.id, quote_amount=6000, eta_h=24)  # +22.4%
        rc = tender_ops.close_tender(db, oc, U_DISPATCH, force=True)
        ok(rc["status"] == "failed", "超预算 22% → 直接流标交人", str(rc.get("status")))
        ok("超过可谈上限" in str(rc.get("reason") or ""),
           "流标理由写清为什么没谈", str(rc.get("reason")))

        # ---------------------------------------------------------------- 零投标
        print("\n[DB-6] 零投标：先延期，再流标")
        od, _, _ = _mk_order(db, "SOTEST-D", budget="5000")
        db.commit()
        t_open = tender_ops.open_tender(db, od, U_DISPATCH)
        rd = tender_ops.close_tender(db, od, U_DISPATCH, force=True)
        ok(rd["status"] == "open" and rd.get("round_no") == 2,
           "零投标 → 延期一轮，仍在收标", str(rd))
        dd = db.get(Tender, t_open["tender_id"])
        ok(dd.deadline_at > datetime.now(), "延期后截止时间往后推了", str(dd.deadline_at))
        rd2 = tender_ops.close_tender(db, od, U_DISPATCH, force=True)
        ok(rd2["status"] == "failed", "延期后仍零投标 → 流标", str(rd2.get("status")))

        # ---------------------------------------------------------------- 视角
        print("\n[DB-7] 承运商看不到别家的报价")
        v = tender_ops.view(db, oa.id, viewer_org=ORG_A)
        ok("bids" not in v, "承运商视角没有别家的报价列表", str(list(v))[:120])
        ok(v.get("bid_count") == 2, "但能看到有几家在争", str(v.get("bid_count")))
        ok((v.get("my_bid") or {}).get("quote") == 4800.0, "能看到自己的报价", str(v.get("my_bid")))
        ok(v.get("won") is True, "中标状态对自己可见", str(v.get("won")))
        vd = tender_ops.view(db, oa.id)
        ok(len(vd.get("bids") or []) == 2, "调度视角能看到两家报价与分数",
           str(len(vd.get("bids") or [])))
    finally:
        db.rollback()
        _cleanup(db)
        db.close()
    print("\n  （测试数据已清理）")


# ---------------------------------------------------------------------------
# S3：协商还价（调度 ↔ 承运商 消息往复）
# ---------------------------------------------------------------------------
def check_negotiate_db() -> None:
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from sca.platform.agents import negotiation
    from sca.platform.db import session_as
    from sca.platform.models import (AgentMessage, CarrierBid, Notification,
                                     ShipperOrder, Tender)
    from sca.platform.services import tender_ops
    from sca.platform.services import tender as rules

    print("\n[NEG-0] 调度侧还价规则（纯函数）")
    d = rules.dispatch_reply(offer="4900", counter="4800", round_no=1)
    ok(d["action"] == "accept", "承运商回价不高于上限 → 成交", str(d))
    d = rules.dispatch_reply(offer="4900", counter="5300", round_no=1)
    ok(d["action"] == "counter" and d["amount"] == Decimal("4900.00"),
       "首轮回价超上限 → 重申上限（调度的唯一筹码就是预算）", str(d))
    d = rules.dispatch_reply(offer="4900", counter="5300", round_no=2)
    ok(d["action"] == "fail" and "无新筹码" in d["reason"],
       "第二轮仍超上限 → 无交集判死（不是态度，是区间不相交）", str(d))
    d = rules.dispatch_reply(offer="4900", counter="5300", round_no=3)
    ok(d["action"] == "fail" and "轮上限" in d["reason"],
       "轮次用尽 → 判死", str(d))

    db = session_as("app")
    try:
        _cleanup(db)
        # 成本 4700（1000km × 4.70）：底线约 4796 < 上限 4900，让得起
        _mk_carrier(db, ORG_A, "协商甲", rate="4.70", pos="石家庄", rating="4.50")
        # 成本 5200（1000km × 5.20）：底线约 5306 > 上限 4900，让不起
        _mk_carrier(db, ORG_B, "协商乙", rate="5.20", pos="石家庄", rating="4.50")
        db.commit()

        # ------------------------------------------------------------ 可谈成交
        print("\n[NEG-1] 超预算可谈 → 还价后成交")
        o, plan, pub = _mk_order(db, "SOTEST-N1", budget="5000")
        db.commit()
        tender_ops.open_tender(db, o, U_DISPATCH)
        b = tender_ops.submit_bid(db, U_A, o.id, quote_amount=5300, eta_h=24)
        floor = Decimal(str(b["floor"]))
        ok(floor < pub.publish_price,
           "承运商成本底线低于可付上限 → 这单谈得成", f"floor={floor}")
        res = tender_ops.close_tender(db, o, U_DISPATCH, force=True)
        ok(res["status"] == "negotiating",
           "超预算 8% 且在上限 15% 内 → 进入协商而不是流标", str(res.get("status")))
        try:
            tender_ops.submit_bid(db, U_A, o.id, quote_amount=4700, eta_h=24)
            ok(False, "协商中不能再投放新报价（否则绕过谈判轨迹）", "居然投成功了")
        except tender_ops.TenderError as exc:
            ok("协商" in str(exc), "协商中不能再投放新报价（否则绕过谈判轨迹）", str(exc))

        t = tender_ops.active_tender(db, o.id)
        out = negotiation.advance(db, o, t, user=U_DISPATCH)
        ok(out["status"] == "awarded", "还价后达成一致 → 定标", str(out.get("status")))
        ok(Decimal(str(out["final_amount"])) == pub.publish_price,
           "成交价就是可付上限，没有超预算付款", f"{out.get('final_amount')}")
        turns = out["turns"]
        ok(len(turns) == 2 and turns[0]["from"] == "dispatch"
           and turns[1]["from"] == "carrier" and turns[1]["type"] == "accept",
           "轨迹是「调度还价 → 承运商接受」两条，不多不少", str(turns))
        db.refresh(o)
        db.refresh(pub)
        ok(o.status == "candidate", "协商成交只推进到 candidate，接单仍由承运商确认",
           o.status)
        ok(pub.publish_price == Decimal("4900.00"),
           "挂牌价没被协商改写（金额链不动）", str(pub.publish_price))
        bid = db.execute(select(CarrierBid).where(
            CarrierBid.order_id == o.id,
            CarrierBid.carrier_org_id == ORG_A)).scalars().first()
        ok(bid.quote_amount == Decimal("4900.00"),
           "承运商的最终报价被更新为成交价", str(bid.quote_amount))
        db.refresh(t)
        ok(t.winner_bid_id == bid.id and t.status == "awarded",
           "招标单记下中标 bid", f"{t.winner_bid_id} vs {bid.id}")
        ok(((t.arbitration or {}).get("outcome") or {}).get("type") == "negotiated",
           "裁决证据里标明这是谈出来的，不是收标直接定的", str(t.arbitration))
        n_to_carrier = db.execute(select(Notification).where(
            Notification.order_id == o.id,
            Notification.ntype == "negotiation")).scalars().all()
        ok(len(n_to_carrier) >= 1, "还价给承运商发了站内通知（它得知道有人出价）",
           str(len(n_to_carrier)))

        # ------------------------------------------------------------ 让不起
        print("\n[NEG-2] 让不起 → 无交集，交人而不是硬谈")
        o2, plan2, pub2 = _mk_order(db, "SOTEST-N2", budget="5000")
        db.commit()
        tender_ops.open_tender(db, o2, U_DISPATCH)
        b2 = tender_ops.submit_bid(db, U_B, o2.id, quote_amount=5600, eta_h=24)
        ok(Decimal(str(b2["floor"])) > pub2.publish_price,
           "底线本身高于可付上限 → 数学上不可能成交",
           f"floor={b2['floor']} 上限={pub2.publish_price}")
        res2 = tender_ops.close_tender(db, o2, U_DISPATCH, force=True)
        ok(res2["status"] == "negotiating", "先按规则进入协商", str(res2.get("status")))
        t2 = tender_ops.active_tender(db, o2.id)
        out2 = negotiation.advance(db, o2, t2, user=U_DISPATCH)
        ok(out2["status"] == "failed", "谈不成 → 流标", str(out2.get("status")))
        ok(out2["outcome"] == "failed", "协商结论标记为 failed", str(out2.get("outcome")))
        ok(float(out2.get("gap") or 0) > 0, "记下缺口金额，人一眼知道差多少",
           str(out2.get("gap")))
        types = [x["type"] for x in out2["turns"]]
        ok("counter" in types and types[-1] == "fail",
           "轨迹里有承运商回价，最后一条是调度判死", str(types))
        db.refresh(o2)
        ok(o2.status == "published", "流标后订单留在 published 等人处置", o2.status)
        ok(o2.budget_max == Decimal("5000.00"),
           "没有自动提预算（price_increase 属人审项）", str(o2.budget_max))
        esc = db.execute(select(AgentMessage).where(
            AgentMessage.thread_id == f"order:{o2.id}",
            AgentMessage.msg_type == "escalate")).scalars().all()
        ok(len(esc) == 1 and esc[0].to_agent == "shipper",
           "无交集时向货主发 escalate（只建议，不执行）",
           str([(e.from_agent, e.to_agent) for e in esc]))

        # ------------------------------------------------------------ 隔离
        print("\n[NEG-3] 协商轨迹只给当事人看")
        v_other = tender_ops.view(db, o2.id, viewer_org=ORG_A)
        ok("negotiation" not in v_other,
           "没被还价的承运商看不到谈判轨迹（那是别家的定价空间）",
           str(list(v_other.keys())))
        v_self = tender_ops.view(db, o2.id, viewer_org=ORG_B)
        ok("negotiation" in v_self and v_self["negotiation"]["turns"],
           "被还价的那家能看到自己的谈判轨迹", str(v_self.get("negotiation")))

        # ------------------------------------------------------------ 超时
        print("\n[NEG-4] 协商超时 → 判死，不留悬挂")
        o3, plan3, pub3 = _mk_order(db, "SOTEST-N3", budget="5000")
        db.commit()
        tender_ops.open_tender(db, o3, U_DISPATCH)
        tender_ops.submit_bid(db, U_A, o3.id, quote_amount=5300, eta_h=24)
        tender_ops.close_tender(db, o3, U_DISPATCH, force=True)
        t3 = tender_ops.active_tender(db, o3.id)
        neg3 = tender_ops.negotiation_of(t3)
        neg3["awaiting_since"] = (datetime.now() - timedelta(
            seconds=tender_ops.counter_timeout_s(db) + 60)).isoformat(timespec="seconds")
        t3.negotiation = neg3
        db.commit()
        expired = tender_ops.expire_stale(db)
        ok(any(e["tender_id"] == t3.id for e in expired),
           "超时被扫描到并判死", str([e.get("tender_id") for e in expired]))
        db.refresh(t3)
        ok(t3.status == "failed", "超时的招标单进入终态", t3.status)
        ok((t3.negotiation or {}).get("outcome") == "timeout",
           "结论写明是超时，不是\"谈崩了\"", str((t3.negotiation or {}).get("outcome")))

        print("\n  （协商测试数据已清理）")
    finally:
        _cleanup(db)
        db.close()


# ---------------------------------------------------------------------------
def check_shipper() -> None:
    print("\n[货主：提价建议只是建议，实际改预算属人审项]")
    r = T.shipper_advice(gap=Decimal("250"), budget=Decimal("5000"))
    ok(r["advice"] == "accept", "补 5% 在上限内 → 建议接受", r["advice"])

    r = T.shipper_advice(gap=Decimal("750"), budget=Decimal("5000"))
    ok(r["advice"] == "reject", "补 15% 超过上限 → 建议拒绝", r["advice"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="rules",
                    choices=["rules", "db", "negotiate", "all"],
                    help="rules=纯函数（S0）；db=开标/投标/裁决接线（S2）；"
                         "negotiate=协商往复（S3）；all=全部（需数据库）")
    args = ap.parse_args()

    check_quote()
    check_filter()
    check_score()
    check_decide()
    check_negotiation()
    check_shipper()
    if args.stage in ("db", "all"):
        check_db()
    if args.stage in ("negotiate", "all"):
        check_negotiate_db()

    print(f"\n=== {PASSED} passed, {len(FAILS)} failed ===")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
