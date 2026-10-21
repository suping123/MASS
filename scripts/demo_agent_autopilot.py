"""演示：四个 agent 在跑的时候，没有任何人点"裁决"，单子自己走到了定标。

    ./.venv/Scripts/python.exe scripts/demo_agent_autopilot.py
    ./.venv/Scripts/python.exe scripts/demo_agent_autopilot.py --keep

前置：
  1. 服务已起（start_all.bat / start_platform.bat）
  2. 四个 agent 已起（start_agents.bat），`start_agents.bat --status` 全是"在跑"

它做什么：
  下单 → 出方案 → 挂牌（开标）→ 承运商投标 → **把收标截止时间拨到过去**
  → 等 dispatch agent 自己的 due() 扫到 → 看它是不是自己把标定了。

中间那一步"拨表"是为了不用真等 5 分钟。它只改 ``tender.deadline_at``，
不改任何业务状态——所以最后看到的定标确实是 agent 自己做的。

与 smoke 的区别：smoke 验"接口通不通"，这个验"没人操作时系统会不会自己往前推"。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 8000
BASE = f"http://127.0.0.1:{PORT}"


def call(path: str, payload=None, token: str = "", method: str = ""):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or
                                 ("POST" if data is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def login(account: str) -> str:
    import redis as _redis

    from sca.platform import config

    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True,
                              socket_timeout=3)
    for _ in range(3):
        _, body = call("/api/auth/captcha?purpose=login")
        cid = str(body.get("cid") or "")
        code = str(r.get("sca:cap:" + cid) or "")
        st, d = call("/api/auth/login", {"account": account, "password": "123456",
                                         "code": code, "cid": cid})
        if st == 200:
            return d["token"]
    raise SystemExit(f"{account} 登录失败")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留演示订单")
    ap.add_argument("--wait", type=int, default=20, help="等 agent 自己动手的秒数")
    args = ap.parse_args()

    ts = login("shipper1")
    td = login("dispatcher1")
    tc = login("carrier1")

    print("[1] 下单")
    st, d = call("/api/shipper/chat", {"text": "我要发一批货"}, ts)
    sid = d.get("session_id")
    st, d = call("/api/shipper/chat", {
        "session_id": sid,
        "text": "石家庄到南京，普货 10 吨，40 小时内送到，预算 5000 元"}, ts)
    st, d = call("/api/shipper/chat", {"session_id": sid, "text": "确认"}, ts)
    oid = int(d.get("order_id") or 0)
    if not oid:
        print("下单失败：", json.dumps(d, ensure_ascii=False)[:400])
        return 1
    print(f"    订单 id={oid}")

    print("[2] 调度出方案 → 挂牌（挂牌即开标）")
    st, p = call(f"/api/dispatch/orders/{oid}/plan", {}, td)
    st, pb = call(f"/api/dispatch/orders/{oid}/publish", {}, td)
    tid = int(pb.get("tender_id") or 0)
    print(f"    挂牌 tender_id={tid} 可付运费={pb.get('publish_price')}")

    # 用 /bid 而不是 /quote：/quote 是旧版"试算后进候选"，它不会产生
    # 承运商自己的报价；报价必须由承运商 agent 出（S2 起 /bid）。
    print("[3] 承运商投标（自己算价）")
    st, q = call(f"/api/carrier/orders/{oid}/bid", {}, tc)
    print(f"    报价 {q.get('quote')} 成本 {q.get('cost_total')} "
          f"利润 {q.get('profit')} 订单状态={q.get('order_status')}")
    if st != 200:
        print("投标失败：", json.dumps(q, ensure_ascii=False)[:400])
        return 1

    print("[4] 把收标截止时间拨到 1 分钟前（只改时间，不改状态）")
    from sqlalchemy import select

    from sca.platform.db import session_as
    from sca.platform.models import ShipperOrder, Tender

    db = session_as()
    try:
        t = db.get(Tender, tid)
        t.deadline_at = datetime.now() - timedelta(minutes=1)
        db.commit()
        status_before = t.status
    finally:
        db.close()
    print(f"    招标状态仍是 {status_before}——没有任何人调用裁决接口")

    print(f"[5] 等 dispatch agent 自己扫到（最多 {args.wait}s）")
    deadline = time.monotonic() + args.wait
    final = None
    while time.monotonic() < deadline:
        db = session_as()
        try:
            t = db.get(Tender, tid)
            o = db.get(ShipperOrder, oid)
            final = (t.status, o.status if o else "")
        finally:
            db.close()
        if final[0] in ("awarded", "failed", "cancelled"):
            break
        time.sleep(0.5)

    t_status, o_status = final or ("?", "?")
    print(f"    招标={t_status}  订单={o_status}")
    ok = t_status == "awarded" and o_status == "candidate"
    print("    " + ("PASS  agent 自己完成了定标" if ok else
                    "FAIL  没有自己往前走，先看 start_agents.bat --status"))

    db = session_as()
    try:
        t = db.get(Tender, tid)
        arb = t.arbitration if isinstance(t.arbitration, dict) else {}
        win = arb.get("winner") or {}
        pol = arb.get("policy") or {}
        print("\n[6] 裁决证据")
        print(f"    中标承运商 org={win.get('carrier_org_id')} "
              f"报价={win.get('quote')} 综合分={win.get('score')}")
        print(f"    权重={pol.get('weights')}")
        print(f"    调度目标={pol.get('dispatch_utility')} "
              f"可谈上限={pol.get('negotiate_max_rule', pol.get('negotiate_max_ratio'))}")
        for c in (arb.get("candidates") or [])[:4]:
            print(f"    - org={c.get('carrier_org_id')} 报价={c.get('quote')} "
                  f"分={c.get('score')} 判定={c.get('verdict')} "
                  f"{c.get('reason') or ''}")
    finally:
        db.close()

    if not args.keep:
        print("\n    （演示订单保留，用 --keep 时也一样；不需要就手动在库里删）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
