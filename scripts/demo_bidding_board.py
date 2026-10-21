"""造两单竞价演示数据，让调度台的「竞价」页一打开就有东西可看。

    ./.venv/Scripts/python.exe scripts/demo_bidding_board.py
    ./.venv/Scripts/python.exe scripts/demo_bidding_board.py --clean

前置：服务已起（start_all.bat）+ 四个 agent 已起（start_agents.bat）+
     陪标承运商已建（scripts/seed_demo_carriers.py --apply）。

它造两张单，分别对应页面上两种形态：

* **单 A：收标中** —— 三家各报一个价（便宜但评分低 / 贵一点但快 / 中间），
  不裁决。看板上能看到"3 家 · ¥9,7xx – ¥9,8xx · 还剩 N 分钟"。
* **单 B：谈过一轮才定标** —— 两家都略超上限，收标后进协商、调度重申上限、
  承运商在底线之上接受。看板上能看到"已定标"，
  抽屉里能看到完整的比价表 + 逐轮还价轨迹。

**刻意不伪造结果**：单 A 不裁决（裁决是 dispatch agent 到点自己做的事），
单 B 的成交价由承运商 agent 按自己的成本底线决定，脚本只负责"让分歧发生"。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
PASSWORD = "123456"


def call(path: str, payload=None, token: str = "") -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def solve(resp: dict) -> tuple[str, str]:
    """真实链路里的图形验证码：测试从 Redis 读答案，和真人只差"认图"这一步。"""
    import redis as _redis

    from sca.platform import config

    cid = str((resp or {}).get("cid") or "")
    if not cid:
        return "", ""
    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True,
                              socket_timeout=3)
    return str(r.get("sca:cap:" + cid) or ""), cid


def login(account: str) -> str:
    st, c = call("/api/auth/code", {"account": account, "purpose": "login"})
    if st != 200:
        raise SystemExit(f"{account} 取验证码失败：{st} {c}")
    code, cid = solve(c)
    st, lr = call("/api/auth/login", {"account": account, "password": PASSWORD,
                                      "code": code, "cid": cid})
    if st != 200:
        raise SystemExit(f"{account} 登录失败：{st} {lr}")
    return lr["token"]


def make_order(ship: str, disp: str, text: str) -> tuple[int, float]:
    st, d = call("/api/shipper/chat", {"text": text}, ship)
    sid = d.get("session_id")
    st, d2 = call("/api/shipper/chat", {"session_id": sid, "text": "确认"}, ship)
    oid = int((d2 or {}).get("order_id") or 0)
    if not oid:
        raise SystemExit(f"下单失败：{st} {d2}")
    call(f"/api/dispatch/orders/{oid}/plan", {}, disp)
    st, pb = call(f"/api/dispatch/orders/{oid}/publish", {}, disp)
    if st != 200:
        raise SystemExit(f"挂牌失败：{st} {pb}")
    return oid, float(pb["publish_price"])


def _result(st: int, body: dict) -> str:
    if st == 200:
        return "OK"
    return f"{st} {body.get('detail') or body}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="删掉 --ids 指定的演示单")
    ap.add_argument("--ids", nargs="*", type=int, default=[],
                    help="要删除的订单 id（必须显式给出，不猜）")
    args = ap.parse_args()

    if args.clean:
        return clean(args.ids)

    tokens = {a: login(a) for a in
              ("shipper1", "dispatcher1", "carrier1", "carrier2", "carrier3")}
    ship, disp = tokens["shipper1"], tokens["dispatcher1"]
    carriers = [tokens["carrier1"], tokens["carrier2"], tokens["carrier3"]]

    print("[1] 单 A：三家竞价，保持收标中（不裁决）")
    oid_a, price_a = make_order(ship, disp, "石家庄到南京 普货 10吨 后天 预算1万")
    # 三家的差异刻意做在两个维度上：价格与承诺时效。
    # 报价必须高于各自的成本底线，否则承运商 agent 会拒（这是正确行为）
    bids_a = [(-40, 31), (-15, 26), (10, 24)]
    for tok, (delta, eta) in zip(carriers, bids_a):
        amount = round(price_a + delta, 2)
        st, r = call(f"/api/carrier/orders/{oid_a}/bid",
                     {"quote_amount": amount, "eta_h": eta}, tok)
        print(f"    报价 ¥{amount:,} / {eta}h -> {_result(st, r)}")

    print("\n[2] 单 B：两家略超上限 → 收标进协商 → 谈成")
    oid_b, price_b = make_order(ship, disp, "上海到广州 普货 8吨 大后天 预算1.3万")
    for tok, ratio in ((carriers[0], 1.03), (carriers[1], 1.06)):
        amount = round(price_b * ratio, 2)
        st, r = call(f"/api/carrier/orders/{oid_b}/bid",
                     {"quote_amount": amount, "eta_h": 40}, tok)
        print(f"    报价 ¥{amount:,} -> {_result(st, r)}")
    st, closed = call(f"/api/dispatch/orders/{oid_b}/tender/close?force=true",
                      {}, disp)
    print(f"    收标：{closed.get('status')} · {closed.get('reason')}")
    if (closed.get("status") or "") == "negotiating":
        st, run = call(f"/api/dispatch/orders/{oid_b}/tender/negotiate", {}, disp)
        print(f"    还价推进：{run.get('status')} · 走了 {run.get('rounds_used')} 轮 · "
              f"{run.get('reason') or ''}")

    st, board = call("/api/dispatch/tenders?status=active", token=disp)
    st2, done = call("/api/dispatch/tenders?status=awarded", token=disp)
    print("\n[3] 看板现在长这样")
    for it in (board.get("items") or []) + (done.get("items") or [])[:3]:
        print(f"    {it['order_no']}  {it['origin']}→{it['dest']}  "
              f"{it['status_label']}  报价 {it['bid_count']} 家  "
              f"区间 {it['min_quote']}–{it['max_quote']}  上限 {it['budget']}")
    print("\n打开 http://127.0.0.1:8000/dispatch.html → 「竞价」页；"
          "点订单号或「比价」看比价表与还价轨迹。")
    print(f"清理：./.venv/Scripts/python.exe scripts/demo_bidding_board.py "
          f"--clean --ids {oid_a} {oid_b}")
    return 0


def clean(ids: list[int]) -> int:
    """删掉指定订单及其关联行。

    **必须显式给 id**：这个脚本里唯一能证明"是演示单"的东西就是它自己打印的号，
    按时间或按前缀猜着删，迟早删到真实业务数据。
    """
    if not ids:
        print("请给出要删的订单 id：--clean --ids 123 124")
        return 1
    from sqlalchemy import delete, select

    from sca.platform import models as M
    from sca.platform.db import session_as

    db = session_as("app")
    try:
        exist = [row[0] for row in db.execute(
            select(M.ShipperOrder.id).where(M.ShipperOrder.id.in_(ids))).all()]
        # 顺序按外键依赖：publish 引用 plan，plan 必须在 publish 之后删
        for model in (M.Tender, M.CarrierBid, M.OrderEvent, M.ExceptionReport,
                      M.Notification, M.Settlement, M.ChatSession, M.OrderPublish,
                      M.OrderPlan):
            db.execute(delete(model).where(model.order_id.in_(ids)))
        db.execute(delete(M.AgentMessage).where(
            M.AgentMessage.thread_id.in_([f"order:{i}" for i in ids])))
        db.execute(delete(M.ShipperOrder).where(M.ShipperOrder.id.in_(ids)))
        db.commit()
        print(f"已删除订单 {exist}；剩余订单 {db.query(M.ShipperOrder).count()} 张")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
