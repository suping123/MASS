"""写接口的 HTTP 层验证（进程内，不启动 uvicorn、不占 8000 端口）。

    ./.venv/Scripts/python.exe scripts/test_no_bus.py     # 移除交互（先跑这个）
    ./.venv/Scripts/python.exe scripts/test_negotiate_http.py

分工：业务正确性由 `test_no_bus.py` 与 `test_tender.py` 覆盖；这里只管
**接口这一层**——路由有没有注册、鉴权有没有生效、三种失败有没有分清楚：

    401  没登录
    403  角色不对（承运商不能挂牌、调度不能接单）
    409  业务不行（有 Agent 回复的 reject，原因是人话）
    504  没人接活（Agent 进程没起，提示去 start_agents.bat）

S5 之后 `POST /api/dispatch/agents/pump`、`/api/dispatch/agent/tick`、
`/api/carrier/agent/tick` 这三个"推一轮"的入口已经删除：消息由常驻进程自己消费，
留着它们等于允许"在 HTTP 里把四个 agent 依次跑一遍"，那是最容易退回去的单控制流。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from sca.platform import config  # noqa: E402
from sca.platform.main import app  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agent_runtime as agent_rt  # noqa: E402

os.environ.setdefault("SCA_AGENT_WAIT_S", "1.0")

FAILS: list[str] = []
PASSED = 0

# 本套件会造一张真订单（挂牌 + 接单）。不清理的话，它会以"收标中 · 0 家报价"
# 的形态长期留在竞价看板上——测试数据混进演示数据，比测试失败更难发现。
_created: list[int] = []


def _cleanup() -> None:
    if not _created:
        return
    from sqlalchemy import delete

    from sca.platform import models as M
    from sca.platform.db import session_as

    db = session_as("app")
    try:
        for model in (M.Tender, M.CarrierBid, M.OrderEvent, M.ExceptionReport,
                      M.Notification, M.Settlement, M.ChatSession, M.OrderPublish,
                      M.OrderPlan):
            db.execute(delete(model).where(model.order_id.in_(_created)))
        db.execute(delete(M.AgentMessage).where(
            M.AgentMessage.thread_id.in_([f"order:{i}" for i in _created])))
        db.execute(delete(M.ShipperOrder).where(M.ShipperOrder.id.in_(_created)))
        db.commit()
    finally:
        db.close()

DEAD_PATHS = ("/api/dispatch/agents/pump", "/api/dispatch/agent/tick",
              "/api/carrier/agent/tick")
LIVE_PATHS = ("/api/dispatch/orders/{order_id}/publish",
              "/api/carrier/orders/{order_id}/accept",
              "/api/carrier/orders/{order_id}/bid",
              "/api/dispatch/agents/thread")


def ok(cond, name: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


def solve_captcha(resp) -> tuple[str, str]:
    import redis as _redis

    cid = str((resp or {}).get("cid") or "")
    if not cid:
        return "", ""
    r = _redis.Redis.from_url(config.REDIS_URL, decode_responses=True,
                             socket_timeout=3)
    return str(r.get("sca:cap:" + cid) or ""), cid


def login(c: TestClient, account: str) -> str:
    for _ in range(3):
        body = c.get("/api/auth/captcha", params={"purpose": "login"}).json()
        code, cid = solve_captcha(body)
        if not code:
            raise RuntimeError(f"{account} 验证码答案不在 Redis（cid={cid}）")
        r = c.post("/api/auth/login", json={"account": account,
                                           "password": "123456",
                                           "code": code, "cid": cid})
        if r.status_code == 200:
            return r.json()["token"]
    raise RuntimeError(f"{account} 登录失败：{r.status_code} {r.text[:160]}")


def main() -> int:
    c = TestClient(app)
    paths = sorted(app.openapi().get("paths") or {})

    print("[1] 路由清单：写接口还在，'推一轮'的入口已删除")
    for p in LIVE_PATHS:
        ok(p in paths, f"接口已注册 {p}")
    for p in DEAD_PATHS:
        ok(p not in paths, f"单进程推进入口已删除 {p}", str([x for x in paths if "tick" in x or "pump" in x]))

    print("\n[2] 未登录一律 401")
    for p, body in (("/api/carrier/orders/1/accept", {"position_text": "石家庄"}),
                    ("/api/dispatch/orders/1/publish", {}),
                    ("/api/carrier/orders/1/bid", {})):
        r = c.post(p, json=body)
        ok(r.status_code == 401, f"{p} 未登录被拒", str(r.status_code))

    agents = agent_rt.start()
    try:
        td = login(c, "dispatcher1")
        tc = login(c, "carrier1")
        ts = login(c, "shipper1")

        print("\n[3] 角色不能串台")
        r = c.post("/api/dispatch/orders/1/publish", json={},
                   headers={"Authorization": f"Bearer {tc}"})
        ok(r.status_code == 403, "承运商不能挂牌", str(r.status_code))
        r = c.post("/api/carrier/orders/1/accept", json={"position_text": "石家庄"},
                   headers={"Authorization": f"Bearer {td}"})
        ok(r.status_code == 403, "调度员不能替承运商接单", str(r.status_code))
        r = c.post("/api/carrier/orders/1/accept", json={"position_text": "石家庄"},
                   headers={"Authorization": f"Bearer {ts}"})
        ok(r.status_code == 403, "货主不能接单", str(r.status_code))

        print("\n[4] 业务拒绝 -> 409，原因是人话（由 Agent 回的 reject）")
        r = c.post("/api/shipper/chat", json={
            "text": "石家庄到南京 普货 10吨 40小时内送到 预算5000元"},
            headers={"Authorization": f"Bearer {ts}"})
        sid = r.json().get("session_id")
        r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"},
                   headers={"Authorization": f"Bearer {ts}"})
        oid = int((r.json() or {}).get("order_id") or 0)
        ok(oid > 0, "下单（走 shipper agent）", str(r.json())[:120])
        if oid:
            _created.append(oid)

        r = c.post(f"/api/dispatch/orders/{oid}/plan", json={},
                   headers={"Authorization": f"Bearer {td}"})
        r = c.post(f"/api/dispatch/orders/{oid}/publish", json={},
                   headers={"Authorization": f"Bearer {td}"})
        ok(r.status_code == 200, "挂牌（走 dispatch agent）", f"{r.status_code} {r.text[:120]}")
        r = c.post(f"/api/carrier/orders/{oid}/accept",
                   json={"position_text": "石家庄正定"},
                   headers={"Authorization": f"Bearer {tc}"})
        ok(r.status_code == 200, "接单（走 carrier agent）", f"{r.status_code} {r.text[:120]}")
        r = c.post(f"/api/carrier/orders/{oid}/accept",
                   json={"position_text": "重复接单"},
                   headers={"Authorization": f"Bearer {tc}"})
        detail = str((r.json() or {}).get("detail") or "")
        ok(r.status_code == 409 and "接走" in detail,
           "重复接单 -> 409，原因来自承运商 agent 的 reject", f"{r.status_code} {detail[:80]}")

        print("\n[5] 没人接活 -> 504，提示指向 start_agents.bat")
        agents.stop()
        agent_rt.flush_human()
        agent_rt.clear_inbox("dispatch", "shipper")
        r = c.post(f"/api/dispatch/orders/{oid}/publish", json={},
                   headers={"Authorization": f"Bearer {td}"})
        detail = str((r.json() or {}).get("detail") or "")
        ok(r.status_code == 504, "调度 agent 不在 -> 504", str(r.status_code))
        ok("start_agents.bat" in detail, "504 文案告诉运维去启动什么", detail[:140])

        print("\n[6] Agent 之间仍有协作记录可回放")
        r = c.get("/api/dispatch/agents/thread", params={"order_id": oid},
                  headers={"Authorization": f"Bearer {td}"})
        body = r.json() or {}
        types = [(m.get("from"), m.get("to"), m.get("type"))
                 for m in (body.get("messages") or [])]
        ok(r.status_code == 200 and any(t[2] == "invoke" for t in types),
           "回放里能看到人类的 invoke 委托", str(types)[:200])
    finally:
        try:
            agents.stop()
        except Exception:                       # noqa: BLE001
            pass
        _cleanup()

    print(f"\n=== {PASSED} passed, {len(FAILS)} failed ===")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
