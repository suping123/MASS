"""货主端 Agent 的进程内验证：缺信息反问 -> 补全 -> 复述 -> 确认 -> 落单。

    ./.venv/Scripts/python.exe scripts/test_platform_shipper.py
"""
from __future__ import annotations

import sys
import time
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
from sca.platform.models import ChatMessage, ChatSession, OrderEvent, ShipperOrder  # noqa: E402

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

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()

    created: list[int] = []
    with TestClient(app) as c:
        tk = login(c, "shipper1")
        H = {"Authorization": f"Bearer {tk}"}

        print("\n[1] 信息不足时要反问，而不是替用户编")
        r = c.post("/api/shipper/chat", json={"text": "我要发一批货"}, headers=H)
        check("首轮返回 200", r.status_code == 200, r.text[:160])
        d = r.json()
        check("停在收集态", d.get("state") == "collecting", d.get("state"))
        check("明确列出缺失项", len(d.get("missing") or []) >= 3, str(d.get("missing")))
        check("反问里出现缺失项名称", "起运地" in d["reply"] or "目的地" in d["reply"],
              d.get("reply", "")[:120])
        check("未落单", d.get("done") is False and not d.get("order_id"), str(d.get("order_id")))
        sid = d["session_id"]

        print("\n[2] 补齐信息后先复述，不直接执行")
        r = c.post("/api/shipper/chat", json={
            "session_id": sid,
            "text": "石家庄到南京，冷链 8 吨，明天下午6点前送到，预算1.2万",
        }, headers=H)
        d = r.json()
        check("进入确认态", d.get("state") == "confirming", f"{d.get('state')} {d.get('reply')[:80]}")
        check("复述含全部六项", len((d.get("data") or {}).get("summary") or []) == 6,
              str(d.get("data")))
        check("复述文本要求确认", "确认" in d.get("reply", ""), d.get("reply", "")[:80])
        check("此时仍未落单", d.get("done") is False, str(d.get("order_id")))
        slots = d.get("slots") or {}
        check("起讫解析正确", slots.get("origin") and slots.get("dest"),
              f"{slots.get('origin')}->{slots.get('dest')}")
        check("重量解析为 8", str(slots.get("weight_t")) in ("8", "8.0"), str(slots.get("weight_t")))
        check("预算解析为 12000", str(slots.get("budget_max")) in ("12000", "12000.0"),
              str(slots.get("budget_max")))
        check("货类识别为冷链", "冷" in str(slots.get("cargo_type")), str(slots.get("cargo_type")))

        print("\n[3] 用户确认后才落单")
        r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H)
        d = r.json()
        check("落单成功", d.get("done") is True and d.get("order_id"), str(d)[:160])
        oid = d.get("order_id")

        r = c.get(f"/api/shipper/chat/{sid}", headers=H)
        hist = r.json() or []
        check("会话历史可回读（刷新页面能恢复对话）",
              r.status_code == 200 and len(hist) >= 4
              and any(m.get("role") == "user" for m in hist),
              f"{r.status_code} {str(hist)[:180]}")
        if oid:
            created.append(oid)
            db = session_as("app")
            try:
                o = db.get(ShipperOrder, oid)
                check("订单状态为 submitted", o is not None and o.status == "submitted",
                      o.status if o else "None")
                check("预算已落库且为 12000",
                      o is not None and o.budget_max is not None and float(o.budget_max) == 12000.0,
                      str(o.budget_max) if o else "None")
                evs = db.query(OrderEvent).filter(OrderEvent.order_id == oid).order_by(
                    OrderEvent.seq).all()
                check("事件流两条：创建 + 提交", [e.event_type for e in evs] ==
                      ["order_created", "order_submitted"], str([e.event_type for e in evs]))
                check("事件序号从 1 递增", [e.seq for e in evs] == [1, 2], str([e.seq for e in evs]))
            finally:
                db.close()

        print("\n[4] 确认态说「不对」要能改，且改完重新复述")
        r = c.post("/api/shipper/chat", json={
            "text": "石家庄到南京 普货 5 吨 后天下午3点前 预算8000"}, headers=H)
        sid2 = r.json()["session_id"]
        check("第二个会话进入确认态", r.json().get("state") == "confirming",
              str(r.json().get("state")) + " " + str(r.json().get("missing")))
        r = c.post("/api/shipper/chat", json={"session_id": sid2, "text": "不对，重量改成10吨"},
                   headers=H)
        d = r.json()
        check("否定后回到收集/确认流程", d.get("state") in ("collecting", "confirming"),
              str(d.get("state")))
        check("重量被更新为 10", str((d.get("slots") or {}).get("weight_t")) in ("10", "10.0"),
              str((d.get("slots") or {}).get("weight_t")))
        check("起讫未被误改", (d.get("slots") or {}).get("origin") == "石家庄",
              str((d.get("slots") or {}).get("origin")))

        print("\n[5] 货主查询：只看自己的单")
        r = c.get("/api/shipper/orders", headers=H)
        check("订单列表 200", r.status_code == 200, r.text[:120])
        lst = r.json() or []
        check("能看到刚提交的单", any(o["id"] == oid for o in lst), str([o["id"] for o in lst]))
        check("列表不含预算字段", all("budget_max" not in o for o in lst),
              str(lst[:1])[:160])

        r = c.get(f"/api/shipper/orders/{oid}", headers=H)
        check("详情 200 且带事件", r.status_code == 200 and len(r.json().get("events") or []) == 2,
              str(r.json().get("events"))[:120])

        print("\n[6] 越权：别的角色不能调货主接口")
        tk_d = login(c, "dispatcher1")
        r = c.get("/api/shipper/orders", headers={"Authorization": f"Bearer {tk_d}"})
        check("调度员无 order:read_own 被拒（403）", r.status_code == 403,
              f"{r.status_code} {r.text[:100]}")
        tk_c = login(c, "carrier1")
        r = c.get("/api/shipper/orders", headers={"Authorization": f"Bearer {tk_c}"})
        check("承运商无 order:read_own 被拒（403）", r.status_code == 403,
              f"{r.status_code} {r.text[:100]}")

        print("\n[7] 轮次上限：空转不会无限消耗")
        r = c.post("/api/shipper/chat", json={"text": "嗯"}, headers=H)
        sid3 = r.json()["session_id"]
        last = None
        for _ in range(14):
            last = c.post("/api/shipper/chat",
                          json={"session_id": sid3, "text": "嗯"}, headers=H).json()
            if last.get("done"):
                break
        check("超过轮次上限后终止", last.get("done") is True and last.get("state") == "aborted",
              f"{last.get('state')} rounds={last.get('rounds')}")

    # 清理测试产生的会话与订单
    db = session_as("app")
    try:
        for oid in created:
            db.query(OrderEvent).filter(OrderEvent.order_id == oid).delete()
            db.delete(db.get(ShipperOrder, oid))
        ids = [s.id for s in db.query(ChatSession).filter(
            ChatSession.user_id == 1).all()]
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
    agents.stop()
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
