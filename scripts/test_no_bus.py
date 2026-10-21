"""第 5 条检验：移除交互，任务必须做不成。

    ./.venv/Scripts/python.exe scripts/test_no_bus.py

这是整个改造最要紧的一个测试。S5 之前，接单/挂牌/投标都在 HTTP 请求内直接调
服务函数——把四个 agent 进程全部杀掉，业务照样从下单走到送达，那"四个独立
agent"就只是个说法。这个文件就是用来证伪那个说法的：

  [A] 起 agent：下单 -> 出方案 -> 挂牌 全部走通（证明链路本身没坏）
  [B] 停 agent：同样的写操作必须 504，且**库里什么都没变**
  [C] 起来之后：同样的动作又能做成，且 `agent_message` 里留有 invoke 记录

[B] 里除了状态码，还要查库：只看 504 不够——万一接口先写库、再超时返回，
那"移除交互"仍然是假的。所以要断言的是一手交钱一手交货：没人执行 = 没发生。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from sca.platform import security  # noqa: E402
from sca.platform.agents import bus, handoff  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
from sca.platform.models import (AgentMessage, CarrierBid, OrderPublish,  # noqa: E402
                                 ShipperOrder)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
import _agent_runtime as agent_rt  # noqa: E402

# 等不到回复时不用真等 5 秒：这里测的是"没人接活"，不是"接得慢"
os.environ["SCA_AGENT_WAIT_S"] = "0.6"

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
    r = c.get("/api/auth/captcha", params={"purpose": "login", "account": account})
    code, cid = solve_captcha(r.json())
    r = c.post("/api/auth/login", json={"account": account, "password": "123456",
                                        "code": code, "cid": cid})
    assert r.status_code == 200, r.text
    return (r.json() or {})["token"]


# ---------------------------------------------------------------------------
# 直连库的读数：用来证明"没发生"，而不是"返回了 504"
# ---------------------------------------------------------------------------
def _count(model) -> int:
    db = session_as("app")
    try:
        return int(db.execute(select(func.count()).select_from(model)).scalar() or 0)
    finally:
        db.close()


def _status(oid: int) -> str:
    db = session_as("app")
    try:
        return str(db.execute(select(ShipperOrder.status).where(
            ShipperOrder.id == oid)).scalar() or "")
    finally:
        db.close()


def _invokes(oid: int) -> list[str]:
    """这笔业务上人类委托过哪些动作。"""
    db = session_as("app")
    try:
        rows = db.execute(select(AgentMessage).where(
            AgentMessage.msg_type == "invoke",
            AgentMessage.thread_id == f"order:{oid}")).scalars().all()
        return [str((m.payload or {}).get("action")) for m in rows]
    finally:
        db.close()


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}

        print("[A] 起 agent：下单 / 出方案 / 挂牌全走通")
        agents = agent_rt.start()
        try:
            ok_ready = agents.wait_ready(8.0)
            check("四个 agent 都有心跳（进程内线程版）", ok_ready,
                  str(agents.alive_keys()))

            r = c.post("/api/shipper/chat", json={
                "text": "石家庄到南京 普货 10吨 40小时内送到 预算5000元"},
                headers=H_ship)
            sid = r.json().get("session_id")
            r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"},
                       headers=H_ship)
            oid = int((r.json() or {}).get("order_id") or 0)
            check("下单成功（由 shipper agent 执行）", oid > 0, str(r.json())[:160])

            r = c.post(f"/api/dispatch/orders/{oid}/plan", json={}, headers=H_disp)
            check("出方案成功", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
            r = c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)
            check("挂牌成功（由 dispatch agent 执行）", r.status_code == 200,
                  f"{r.status_code} {r.text[:160]}")
            check("挂牌后订单已发布", _status(oid) == "published", _status(oid))

            agents.stop()
            check("agent 线程已全部退出", not agents.running, str(agents.running))

            # 停掉之后先把"人"的残留回复清掉，避免后面的断言看到上一轮的 504 回执
            agent_rt.flush_human()

            print("\n[B] 没有 agent 时：所有成交写操作都必须失败，且库不变")
            n_order0 = _count(ShipperOrder)
            r = c.post("/api/shipper/chat", json={
                "text": "保定到杭州 普货 5吨 明天18点前 预算3000元"}, headers=H_ship)
            sid2 = r.json().get("session_id")
            r = c.post("/api/shipper/chat", json={"session_id": sid2, "text": "确认"},
                       headers=H_ship)
            body = r.json() or {}
            # 对话接口的约定是"永远 200，结果写在 reply 里"（缺槽位、执行失败都一样），
            # 所以这里断言的是"话没说完、单没落"，而不是状态码。
            check("下单没成（货主 agent 不在，会话停在确认态）",
                  r.status_code == 200 and body.get("state") == "confirming"
                  and not body.get("order_id"), f"{r.status_code} {str(body)[:140]}")
            check("下单失败时没有多出一张单", _count(ShipperOrder) == n_order0,
                  f"{n_order0} -> {_count(ShipperOrder)}")
            check("失败提示指向 start_agents.bat",
                  "start_agents.bat" in str(body.get("reply") or ""),
                  str(body.get("reply"))[:160])
            check("会话退回确认态（信息没丢，起来后可直接重试）",
                  body.get("state") == "confirming", str(body.get("state")))

            n_pub0 = _count(OrderPublish)
            r = c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)
            check("挂牌失败（调度 agent 不在）", r.status_code == 504,
                  f"{r.status_code} {r.text[:120]}")
            check("挂牌失败时没有多出一条挂牌记录", _count(OrderPublish) == n_pub0,
                  f"{n_pub0} -> {_count(OrderPublish)}")

            n_bid0 = _count(CarrierBid)
            r = c.post(f"/api/carrier/orders/{oid}/bid", json={"quote_amount": 4800},
                       headers=H_car)
            check("投标失败（承运商 agent 不在）", r.status_code == 504,
                  f"{r.status_code} {r.text[:120]}")
            check("投标失败时没有落 bid", _count(CarrierBid) == n_bid0,
                  f"{n_bid0} -> {_count(CarrierBid)}")

            r = c.post(f"/api/carrier/orders/{oid}/accept",
                       json={"position_text": "石家庄正定"}, headers=H_car)
            check("接单失败（承运商 agent 不在）", r.status_code == 504,
                  f"{r.status_code} {r.text[:120]}")
            check("接单失败时订单还是 published", _status(oid) == "published", _status(oid))

            print("\n[C] 重新起 agent：同样的动作又能做成，并留下 invoke 记录")
            # 上一轮那些 504 的委托还躺在收件箱里。先清掉：真实运维里它们会
            # 因为超过 SCA_AGENT_STALE_S 被判为"委托已过期"而不执行，这里直接清干净。
            agent_rt.clear_inbox(*[f"carrier:{o}" for o in _carrier_orgs()] + [
                "dispatch", "shipper", "exception"])
            agents = agent_rt.start()
            agents.wait_ready(8.0)

            r = c.post(f"/api/carrier/orders/{oid}/accept",
                       json={"position_text": "石家庄正定物流园"}, headers=H_car)
            check("再接单成功", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
            check("订单变为 claimed", _status(oid) == "claimed", _status(oid))

            acts = _invokes(oid)
            check("接单在消息表里留了 invoke 记录", "accept_order" in acts, str(acts))
            check("挂牌也走过 invoke", "publish_order" in acts, str(acts))
        finally:
            try:
                agents.stop()
            except Exception:                   # noqa: BLE001
                pass

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过（移除交互后确实做不成）")
    return 0


def _carrier_orgs() -> list[int]:
    from sca.platform.models import CarrierProfile

    db = session_as("app")
    try:
        return [int(x) for x in db.execute(select(CarrierProfile.org_id)).scalars().all()
                if x]
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
