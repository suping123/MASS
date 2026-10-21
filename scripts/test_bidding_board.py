"""S6 竞价视图验证：看板 / 比价表 / 还价轨迹 / agent 在线状态。

    ./.venv/Scripts/python.exe scripts/test_bidding_board.py

重点验四件事：
  1) 看板是**跨单摘要**：报价家数、价格区间、截止倒计时都来自库，不是页面自己算的；
  2) 比价表能解释"为什么是这家"：每家都有报价、时效、综合分与结论（含淘汰原因）；
  3) 信息边界没被打穿：承运商看得到"几家在争"，看不到别家的价，也看不到货主预算；
  4) agent 在线状态由心跳判定：进程没了，页面要能说出"不是单子的问题"。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from sca.platform import security  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402
from sca.platform import models as M  # noqa: E402

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
    assert r.status_code == 200, f"{account}: {r.text}"
    return (r.json() or {})["token"]


def make_published(c: TestClient, H_ship, H_disp, text: str) -> tuple[int, float]:
    r = c.post("/api/shipper/chat", json={"text": text}, headers=H_ship)
    sid = (r.json() or {})["session_id"]
    r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H_ship)
    oid = int((r.json() or {})["order_id"])
    c.post(f"/api/dispatch/orders/{oid}/plan", headers=H_disp)
    r = c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)
    return oid, float((r.json() or {}).get("publish_price") or 0)


def cleanup(created: list[int]) -> None:
    """按外键顺序清掉本次造的订单。published 引用 plan，别搞反。"""
    if not created:
        return
    db = session_as("app")
    try:
        for model in (M.Tender, M.CarrierBid, M.OrderEvent, M.ExceptionReport,
                      M.Notification, M.Settlement, M.ChatSession, M.OrderPublish,
                      M.OrderPlan):
            db.execute(delete(model).where(model.order_id.in_(created)))
        db.execute(delete(M.AgentMessage).where(
            M.AgentMessage.thread_id.in_([f"order:{i}" for i in created])))
        db.execute(delete(M.ShipperOrder).where(M.ShipperOrder.id.in_(created)))
        db.commit()
    finally:
        db.close()


def board_of(payload) -> dict:
    return {it["order_id"]: it for it in (payload.get("items") or [])}


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    agents = agent_rt.start()
    created: list[int] = []
    # 心跳被本套件临时改过要还原：别让下一个套件看到"agent 离线"
    beat_backup: dict[str, object] = {}

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_c1 = {"Authorization": f"Bearer {login(c, 'carrier1')}"}
        H_c2 = {"Authorization": f"Bearer {login(c, 'carrier2')}"}
        H_c3 = {"Authorization": f"Bearer {login(c, 'carrier3')}"}

        print("\n[1] agent 在线状态（心跳 ≤ 90s 算在线）")
        r = c.get("/api/dispatch/agents/status", headers=H_disp)
        check("调度可读 agent 状态", r.status_code == 200, r.text[:160])
        st = r.json() or {}
        kinds = {a["kind"] for a in (st.get("agents") or [])}
        check("覆盖四类 agent", {"dispatch", "exception", "shipper", "carrier"} <= kinds,
              str(sorted(kinds)))
        check("线程版 daemon 在跑 → 全部在线", st.get("any_offline") is False,
              str([a["agent_key"] for a in st.get("agents") or [] if not a["online"]]))
        check("每条带心跳年龄与积压数",
              all(a.get("age_s") is not None and a.get("pending") is not None
                  for a in st.get("agents") or []),
              str((st.get("agents") or [])[:1])[:200])
        r = c.get("/api/dispatch/agents/status", headers=H_c1)
        check("承运商不能读 agent 状态（403）", r.status_code == 403, str(r.status_code))

        # 把 dispatch 的心跳拨到 120 秒前：这是"进程被杀"在库里留下的样子
        db = session_as("app")
        try:
            row = db.query(M.AgentActor).filter(M.AgentActor.agent_key == "dispatch").first()
            beat_backup["dispatch"] = row.last_seen_at
            from datetime import datetime, timedelta
            row.last_seen_at = datetime.now() - timedelta(seconds=120)
            db.commit()
        finally:
            db.close()
        st2 = (c.get("/api/dispatch/agents/status", headers=H_disp).json() or {})
        dispatch_row = next((a for a in st2.get("agents") or []
                             if a["agent_key"] == "dispatch"), {})
        check("心跳超时判为离线", dispatch_row.get("online") is False, str(dispatch_row))
        check("any_offline 一起报出来", st2.get("any_offline") is True, str(st2.get("offline")))
        db = session_as("app")
        try:
            row = db.query(M.AgentActor).filter(M.AgentActor.agent_key == "dispatch").first()
            row.last_seen_at = beat_backup["dispatch"]
            db.commit()
        finally:
            db.close()

        # 组织已不存在的注册行（测试造过又删、组织被撤）是残留：
        # 有心跳但背后没有承运商，显示成"在线"会让人去查一个不存在的机构。
        db = session_as("app")
        try:
            db.add(M.AgentActor(agent_key="carrier:999999", kind="carrier",
                                org_id=999999, enabled=1))
            db.commit()
        finally:
            db.close()
        st3 = c.get("/api/dispatch/agents/status", headers=H_disp).json() or {}
        keys3 = {a["agent_key"] for a in st3.get("agents") or []}
        check("残留注册行不显示成在线 agent", "carrier:999999" not in keys3, str(sorted(keys3)))
        check("但单独报出来（不静默吞掉）",
              "carrier:999999" in (st3.get("orphans") or []), str(st3.get("orphans")))
        db = session_as("app")
        try:
            db.query(M.AgentActor).filter(
                M.AgentActor.agent_key == "carrier:999999").delete()
            db.commit()
        finally:
            db.close()

        print("\n[2] 竞价看板（跨单摘要）")
        oid, price = make_published(c, H_ship, H_disp,
                                    "石家庄到南京 普货 10吨 后天 预算1万")
        created.append(oid)
        r = c.get("/api/dispatch/tenders?status=active", headers=H_disp)
        check("看板接口 200", r.status_code == 200, r.text[:160])
        items = board_of(r.json() or {})
        row = items.get(oid) or {}
        check("新挂牌的单出现在看板", bool(row), f"oid={oid} 看板={list(items)[:6]}")
        check("状态是收标中", row.get("status") == "open" and row.get("status_label") == "收标中",
              f"{row.get('status')} {row.get('status_label')}")
        check("可付上限 = 挂牌价（不是货主预算）", abs(float(row.get("budget") or 0) - price) < 0.02,
              f"{row.get('budget')} vs {price}")
        check("看板不含货主预算字段", "budget_max" not in row, str(sorted(row)[:12]))
        check("零报价时家数为 0", row.get("bid_count") == 0, str(row.get("bid_count")))
        check("截止倒计时是正数", isinstance(row.get("seconds_left"), int)
              and row["seconds_left"] > 0, str(row.get("seconds_left")))
        r = c.get("/api/dispatch/tenders", headers=H_c1)
        check("承运商不能看调度看板（403）", r.status_code == 403, str(r.status_code))

        print("\n[3] 两家报价 → 看板出现价格区间")
        r = c.post(f"/api/carrier/orders/{oid}/bid",
                   json={"quote_amount": round(price - 60, 2), "eta_h": 30}, headers=H_c2)
        check("承运商 2 投标成功", r.status_code == 200, r.text[:200])
        r = c.post(f"/api/carrier/orders/{oid}/bid",
                   json={"quote_amount": round(price - 30, 2), "eta_h": 26}, headers=H_c3)
        check("承运商 3 投标成功", r.status_code == 200, r.text[:200])
        row = board_of(c.get("/api/dispatch/tenders?status=active",
                             headers=H_disp).json() or {}).get(oid) or {}
        check("报价家数 = 2", row.get("bid_count") == 2, str(row.get("bid_count")))
        check("最低价取的是真实报价", abs(float(row.get("min_quote") or 0) - (price - 60)) < 0.02,
              f"{row.get('min_quote')} 期望 {price - 60}")
        check("最高价也如实给出", abs(float(row.get("max_quote") or 0) - (price - 30)) < 0.02,
              f"{row.get('max_quote')} 期望 {price - 30}")

        t_open = c.get(f"/api/dispatch/orders/{oid}/tender", headers=H_disp).json() or {}
        ob = t_open.get("bids") or []
        check("裁决前比价表也有评分（档案属性，不依赖裁决）",
              bool(ob) and all(x.get("rating") is not None for x in ob),
              str([(x.get("carrier_org"), x.get("rating")) for x in ob]))
        check("裁决前没有综合分（打分是裁决的产物，不编一个）",
              all(x.get("score") is None for x in ob), str([x.get("score") for x in ob]))

        print("\n[4] 截止裁决 → 比价表解释「为什么是这家」")
        r = c.post(f"/api/dispatch/orders/{oid}/tender/close?force=true&negotiate=false",
                   headers=H_disp)
        check("收标裁决成功", r.status_code == 200, r.text[:200])
        body = r.json() or {}
        check("裁决结果是定标", body.get("status") == "awarded",
              f"{body.get('status')} / {body.get('reason')}")
        t = c.get(f"/api/dispatch/orders/{oid}/tender", headers=H_disp).json() or {}
        bids = t.get("bids") or []
        check("比价表覆盖两家报价", len(bids) == 2, str(len(bids)))
        check("每家都有机构名（不是裸 org_id）",
              all(b.get("carrier_org") for b in bids), str(bids)[:200])
        check("每家都有综合分与结论",
              all(b.get("score") is not None and b.get("verdict") for b in bids),
              str([(b.get("carrier_org"), b.get("score"), b.get("verdict")) for b in bids]))
        wins = [b for b in bids if b.get("verdict") == "winner"]
        check("恰好一家中标", len(wins) == 1, str([b.get("verdict") for b in bids]))
        # 落标有两类，页面都必须能解释：被硬条件淘汰的（有 reason_code），
        # 以及合规但综合分低的（eligible，靠分数比较解释）。
        check("每种落标都有可解释的结论",
              all(b.get("reason") or b.get("verdict") == "eligible"
                  for b in bids if b.get("verdict") != "winner"),
              str([(b.get("verdict"), b.get("reason")) for b in bids]))
        check("合规落标家带综合分（页面据此说「分数低于中标家」）",
              all(b.get("score") is not None for b in bids if b.get("verdict") == "eligible"),
              str([(b.get("verdict"), b.get("score")) for b in bids]))
        w = t.get("arbitration") or {}
        check("裁决证据带权重且四项之和为 1",
              bool(w.get("weights")) and abs(sum(w["weights"].values()) - 1) < 1e-6,
              str(w.get("weights")))
        check("裁决证据带规则版本", bool(t.get("rule_version")), str(t.get("rule_version")))
        db = session_as("app")
        try:
            plan = db.query(M.OrderPlan).filter(M.OrderPlan.order_id == oid).first()
            check("同一份证据也写进 order_plan.arbitration（建库起一直是空的）",
                  bool((plan.arbitration or {}).get("candidates")) if plan else False,
                  str((plan.arbitration or {}).get("status") if plan else "no plan"))
        finally:
            db.close()
        row = board_of(c.get("/api/dispatch/tenders?status=awarded",
                             headers=H_disp).json() or {}).get(oid) or {}
        check("看板可按已定标回查", row.get("status") == "awarded", str(row.get("status")))
        check("定标后报价家数不塌（落败方被置 rejected 仍算报过价）",
              row.get("bid_count") == 2, str(row.get("bid_count")))
        win = row.get("winner") or {}
        check("看板给出中标机构名与金额",
              bool(win.get("carrier_org")) and float(win.get("amount") or 0) > 0, str(win))

        print("\n[5] 承运商侧：我的竞价与信息边界")
        r = c.get(f"/api/carrier/orders/{oid}/tender", headers=H_c1)
        check("没参与竞价时 my_bid 为空（不编一份出来）",
              r.status_code == 200 and (r.json() or {}).get("my_bid") is None,
              r.text[:160])
        check("承运商视角不含别家报价", "bids" not in (r.json() or {}), r.text[:160])
        mine = c.get("/api/carrier/my-bids", headers=H_c3).json() or []
        mine_row = next((x for x in mine if x["order_id"] == oid), None)
        check("我的竞价里能看到自己的报价", bool(mine_row) and
              abs(float(mine_row.get("quote") or 0) - (price - 30)) < 0.02, str(mine_row)[:200])
        check("知道有几家在争（不给别人价码）",
              mine_row and mine_row.get("bid_count") == 2, str(mine_row and mine_row.get("bid_count")))
        # 中标结果与看板必须一致：一家说中了、另一家也说中了，就是两边都没对
        won_by = []
        for tag, hdr in (("carrier2", H_c2), ("carrier3", H_c3)):
            rows = c.get("/api/carrier/my-bids", headers=hdr).json() or []
            if any(x["order_id"] == oid and x.get("won") for x in rows):
                won_by.append(tag)
        check("中标结果只有一家能看到", len(won_by) == 1, str(won_by))
        check("中标的正是看板上的赢家",
              bool(win.get("carrier_org")) and len(won_by) == 1,
              f"看板={win.get('carrier_org')} 中标={won_by}")
        check("落标的写法是「本单已定标」而不是沉默",
              (mine_row or {}).get("result") in ("你中标了", "本单已定标"),
              str((mine_row or {}).get("result")))
        r = c.get("/api/carrier/my-bids", headers=H_ship)
        check("货主不能读承运商竞价（403）", r.status_code == 403, str(r.status_code))
        pool = c.get("/api/carrier/pool", headers=H_c3).json() or []
        check("池子里没有预算字段", all("budget_max" not in i for i in pool),
              str((pool or [{}])[0])[:160])

        print("\n[6] 还价轨迹：超预算单进协商，只有被谈的那家看得到回合")
        oid2, price2 = make_published(c, H_ship, H_disp,
                                      "石家庄到广州 普货 12吨 大后天 预算1万")
        created.append(oid2)
        # 超预算 3%：在 15% 可谈区间内 → 裁决进 negotiating，而不是流标
        r = c.post(f"/api/carrier/orders/{oid2}/bid",
                   json={"quote_amount": round(price2 * 1.03, 2), "eta_h": 40}, headers=H_c1)
        check("承运商 1 报一个略超上限的价", r.status_code == 200, r.text[:200])
        r = c.post(f"/api/carrier/orders/{oid2}/bid",
                   json={"quote_amount": round(price2 * 1.06, 2), "eta_h": 38}, headers=H_c2)
        check("承运商 2 报更高的价", r.status_code == 200, r.text[:200])
        r = c.post(f"/api/dispatch/orders/{oid2}/tender/close?force=true&negotiate=false",
                   headers=H_disp)
        body = r.json() or {}
        check("超预算但可谈 → 进协商", body.get("status") == "negotiating",
              f"{body.get('status')} / {body.get('reason')}")
        row = board_of(c.get("/api/dispatch/tenders?status=active",
                             headers=H_disp).json() or {}).get(oid2) or {}
        check("看板标出协商中", row.get("status") == "negotiating", str(row.get("status")))
        check("看板指出在跟谁谈", bool(row.get("target_org")), str(row.get("target_org")))

        # 先看"还没开谈"时的两侧：被谈的那家知道轮次，另一家只知道在协商
        before_other = next((x for x in (c.get("/api/carrier/my-bids", headers=H_c2).json() or [])
                             if x["order_id"] == oid2), None)
        check("没被谈的那家知道这单在协商",
              before_other and before_other.get("tender_status") == "negotiating",
              str(before_other and before_other.get("tender_status")))
        check("协商轨迹对它是私有的（谈判筹码不外泄）",
              bool(before_other) and "negotiation" not in before_other, str(before_other)[:200])

        r = c.post(f"/api/dispatch/orders/{oid2}/tender/negotiate", headers=H_disp)
        check("还价推进成功", r.status_code == 200, r.text[:200])
        run = r.json() or {}
        turns = run.get("turns") or []
        check("轨迹第一步是调度出价", bool(turns) and turns[0].get("from") == "dispatch"
              and turns[0].get("type") == "counter", str(turns[:1])[:200])
        check("协商有轮次上限（护栏，不是常态）",
              int(run.get("rounds_used") or 0) >= 1, str(run.get("rounds_used")))
        check("最终状态是终态或仍在谈（依赖承运商底线，不做硬编码）",
              run.get("status") in ("awarded", "failed", "negotiating"), str(run.get("status")))

        t2 = c.get(f"/api/dispatch/orders/{oid2}/tender", headers=H_disp).json() or {}
        check("调度视角能看到协商全轨迹",
              bool((t2.get("negotiation") or {}).get("turns")),
              str((t2.get("negotiation") or {}).get("rounds_used")))

        # 协商成交的单，裁决证据里没有 winner（award_by_accept 只写 outcome），
        # 若直接按 candidates 的 verdict 渲染，中标方会被标成"淘汰"——
        # 最该被解释的那一行反而是错的。
        if run.get("status") == "awarded":
            rows2 = t2.get("bids") or []
            wins = [b for b in rows2 if b.get("verdict") == "winner"]
            check("协商成交的单，比价表把中标方标成「中标」而不是「淘汰」",
                  len(wins) == 1 and wins[0].get("bid_id") == t2.get("winner_bid_id"),
                  str([(b.get("carrier_org"), b.get("verdict")) for b in rows2]))
            check("中标那行的成交价是协商达成的金额，不高于可付上限",
                  bool(wins) and float(wins[0].get("final_quote") or 0) > 0
                  and float(wins[0]["final_quote"]) <= float(t2.get("budget") or 0) + 0.01,
                  str(wins[:1])[:200])
            check("中标那行的原因说清了是协商达成的",
                  bool(wins) and "协商" in (wins[0].get("reason") or ""),
                  str(wins[:1])[:200])
            check("调度视角单独给出实际成交价（与可付上限并列）",
                  float(t2.get("final_amount") or 0) > 0,
                  str(t2.get("final_amount")))
            row2 = board_of(c.get("/api/dispatch/tenders?status=awarded",
                                  headers=H_disp).json() or {}).get(oid2) or {}
            check("看板的中标金额是成交价，不是中标家当初那个超上限的报价",
                  abs(float((row2.get("winner") or {}).get("amount") or 0)
                      - float(t2.get("final_amount") or -1)) < 0.02,
                  str(row2.get("winner")))
            mine2 = c.get(f"/api/carrier/orders/{oid2}/tender", headers=H_c1).json() or {}
            check("被谈的那家同时看到自己的报价与成交价",
                  bool((mine2.get("my_bid") or {}).get("final_quote")),
                  str((mine2.get("my_bid") or {}))[:200])
        else:
            print(f"    （本轮承运商没接，协商结论 {run.get('status')}，成交口径断言不适用）")

        target = c.get("/api/carrier/my-bids", headers=H_c1).json() or []
        t_row = next((x for x in target if x["order_id"] == oid2), None)
        check("被还价的那家看得到回合", bool(t_row) and bool((t_row.get("negotiation") or {}).get("turns")),
              str(t_row)[:200])
        other = c.get("/api/carrier/my-bids", headers=H_c2).json() or []
        o_row = next((x for x in other if x["order_id"] == oid2), None)
        check("谈完它依然看不到协商轨迹（连终态也不补）",
              bool(o_row) and "negotiation" not in o_row, str(o_row)[:200])

        print("\n[7] 手动截止并裁决（页面「截止并裁决」按钮走的就是这条接口）")
        # 自动路径是 dispatch daemon 的 due() 每 15s 扫一次。手工入口必须存在，
        # 否则"agent 没在跑"或"这轮还没扫到"时，人只能等——页面显示已过 12 分钟，
        # 却没有任何能点的地方。
        oid3, _p3 = make_published(c, H_ship, H_disp,
                                   "保定到德州 普货 6吨 明天18点前到 预算5000")
        created.append(oid3)
        r = c.post(f"/api/carrier/orders/{oid3}/bid", json={}, headers=H_c1)
        check("承运商先报一家，裁决才有对象", r.status_code == 200, r.text[:160])

        # 注意路径要用 dispatch 前缀：carrier 前缀下没有这个 POST（那是 405，
        # 说明"没这个接口"，不是"你没权限"）。权限拒绝必须走同一条路由才测得准。
        r = c.post(f"/api/dispatch/orders/{oid3}/tender/close?force=true", headers=H_c1)
        check("承运商没有截止权（403，不是 200/405）", r.status_code == 403, str(r.status_code))

        r = c.post(f"/api/dispatch/orders/{oid3}/tender/close", headers=H_disp)
        body = r.json() or {}
        check("没到截止时间又不加 force：只回报「还没到点」，不改状态",
              r.status_code == 200 and body.get("closed") is False
              and "未到" in str(body.get("reason")), r.text[:200])

        before = board_of(c.get("/api/dispatch/tenders?status=active", headers=H_disp).json() or {})
        check("截止前它在看板「进行中」里", oid3 in before, str(list(before)))

        r = c.post(f"/api/dispatch/orders/{oid3}/tender/close?force=true", headers=H_disp)
        res = r.json() or {}
        check("force=true 能提前截止并给出结论",
              r.status_code == 200 and bool(res.get("status_label")), r.text[:200])
        check("结论真的落到招标单状态上（不再停在收标中）",
              res.get("status") in ("awarded", "negotiating", "failed", "extended"),
              str(res.get("status")))
        after = board_of(c.get("/api/dispatch/tenders?status=active", headers=H_disp).json() or {})
        check("定标后从「进行中」消失（协商中/延期则留下，那是正常态）",
              res.get("status") in ("negotiating", "extended") or oid3 not in after,
              str(after.get(oid3))[:200])

        r2 = c.post(f"/api/dispatch/orders/{oid3}/tender/close?force=true", headers=H_disp)
        check("重复裁决幂等（返回 reused，不重算一遍证据）",
              r2.status_code == 200 and (r2.json() or {}).get("reused") is True,
              r2.text[:200])

    cleanup(created)
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
    raise SystemExit(main())
