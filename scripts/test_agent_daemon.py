"""S4：四进程常驻 / 消息租约 / 门铃 / 到期检查。

    ./.venv/Scripts/python.exe scripts/test_agent_daemon.py
    ./.venv/Scripts/python.exe scripts/test_agent_daemon.py --skip-process

**先停 start_agents.bat**：本脚本会起自己的 carrier/dispatch 进程，抢同一个
收件箱（这与 smoke 的约定一致）。不 --skip-process 时需要 Redis + MySQL 都在。

要证明的事（对应 S4 验收口径）：

1. ``SKIP LOCKED`` 真的能防重复领取——两个连接同时领，同一条消息只能被一个人拿到；
2. 进程被杀 → 租约到期 → 消息能被重领（不是"取走就消失"）；
3. 一条必炸的消息不会堵死收件箱（tries 上限判 failed）；
4. ``await_reply`` 能看到**别的进程**提交的回复（独立短 Session，不被快照困住）；
5. 四个 daemon 各自处理自己的收件箱，互不调用；
6. ``due()`` 闭环：到点裁决 → 进协商 → **立刻发出还价**（不是只改状态）；
7. **故障域**：kill dispatch 之后，carrier 的日志继续长、消息继续被处理。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import delete, func, select  # noqa: E402

from sca.platform.agents import bus, inbox, negotiation, wake  # noqa: E402
from sca.platform.agents.daemons import carrier_daemon, dispatch_daemon  # noqa: E402
from sca.platform.agents.daemons import exception_daemon, shipper_daemon  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.models import (AgentActor, AgentMessage, CarrierBid,  # noqa: E402
                                 CarrierProfile, Notification, OrderEvent,
                                 OrderPlan, OrderPublish, ShipperOrder, SysOrg,
                                 Tender)
from sca.platform.services import tender_ops  # noqa: E402

FAILS: list[str] = []
PASSED = 0

PREFIX = "DAEMONT"
ORG_A = 9911          # 主用例
ORG_B = 9912          # 故障域用例（单独收件箱，避免互相干扰）
U_DISPATCH = {"user_id": 2, "account": "dispatcher1", "role": "dispatcher",
              "org_id": 1}


def ok(cond, name: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 造数 / 清理
# ---------------------------------------------------------------------------
def _mk_carrier(db, org_id: int, name: str, rate: str = "2.60") -> None:
    db.add(SysOrg(id=org_id, name=name, org_type="carrier"))
    db.flush()
    db.add(CarrierProfile(org_id=org_id, cost_per_km=Decimal(rate),
                          capacity_t=Decimal("30"), rating=Decimal("4.50"),
                          current_position="石家庄"))
    db.flush()


def _mk_order(db, suffix: str = "1"):
    o = ShipperOrder(order_no=f"{PREFIX}-{suffix}", shipper_user_id=1, shipper_org_id=2,
                     origin="石家庄", dest="南京", cargo_type="普货",
                     weight_t=Decimal("10"), budget_max=Decimal("5000"),
                     required_by="40小时内",
                     required_by_at=datetime.now() + timedelta(hours=40),
                     status="planned")
    db.add(o)
    db.flush()
    plan = OrderPlan(order_id=o.id, version=1, distance_km=Decimal("1000"),
                     eta_h=Decimal("24"), cost_est=Decimal("2600"),
                     route_json={"provider": "test"})
    db.add(plan)
    db.flush()
    o.active_plan_id = plan.id
    fee = (Decimal("5000") * Decimal("0.02")).quantize(Decimal("0.01"))
    db.add(OrderPublish(order_id=o.id, plan_id=plan.id, gross_price=Decimal("5000"),
                        shipper_fee_rate=Decimal("0.02"), shipper_fee=fee,
                        carrier_fee_rate=Decimal("0.02"),
                        publish_price=Decimal("5000") - fee, status="open",
                        published_by=2))
    o.status = "published"
    db.flush()
    return o


def _cleanup(db) -> None:
    ids = [r[0] for r in db.execute(select(ShipperOrder.id).where(
        ShipperOrder.order_no.like(f"{PREFIX}%"))).all()]
    for oid in ids:
        for model in (Tender, CarrierBid, OrderEvent):
            db.execute(delete(model).where(model.order_id == oid))
        db.execute(delete(AgentMessage).where(
            AgentMessage.thread_id == f"order:{oid}"))
        db.execute(delete(Notification).where(Notification.order_id == oid))
        db.execute(delete(OrderPublish).where(OrderPublish.order_id == oid))
        db.execute(delete(OrderPlan).where(OrderPlan.order_id == oid))
        db.execute(delete(ShipperOrder).where(ShipperOrder.id == oid))
    db.execute(delete(AgentMessage).where(AgentMessage.thread_id == "selftest:daemon"))
    # 注册行必须一起删：真实 carrier 进程的 keys() 会把"注册表里出现过的 org"
    # 也算成自己的收件箱，于是这两个假 org 会被它一直心跳下去，在
    # /api/dispatch/agents/status 里永远显示成"机构已删却在线"的孤儿。
    db.execute(delete(AgentActor).where(AgentActor.org_id.in_((ORG_A, ORG_B))))
    db.execute(delete(CarrierProfile).where(CarrierProfile.org_id.in_((ORG_A, ORG_B))))
    db.execute(delete(SysOrg).where(SysOrg.id.in_((ORG_A, ORG_B))))
    db.commit()


def _clear_inbox(key: str) -> int:
    """清空某个收件箱的历史消息。

    不清的话，上一轮留下的 pending/过期 working 会占满 ``claim(limit)``，
    本轮新造的消息排在最末尾被 limit 截掉——测试会随历史数据时绿时红，
    这种"偶发失败"比直接红更难查。
    """
    db = session_as()
    try:
        n = db.execute(delete(AgentMessage).where(
            AgentMessage.to_agent == key)).rowcount
        db.commit()
        return int(n or 0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# [1] 租约：SKIP LOCKED
# ---------------------------------------------------------------------------
def check_skip_locked(key: str) -> None:
    print("\n[1] 并发领取：同一条消息不会被两个人拿到")
    _clear_inbox(key)
    db = session_as()
    try:
        for i in range(2):
            bus.send(db, from_agent="dispatch", to_agent=key, msg_type="notify",
                     payload={"event": f"selftest-skip-{i}"},
                     thread_id="selftest:daemon", commit=True)
    finally:
        db.close()

    d1, d2 = session_as(), session_as()
    try:
        # 手工拿住第一条（不提交），模拟"进程 A 正在处理"
        stmt = (select(AgentMessage).where(
            AgentMessage.to_agent == key,
            AgentMessage.status == "pending").order_by(
            AgentMessage.id.asc()).limit(1).with_for_update(skip_locked=True))
        locked = list(d1.execute(stmt).scalars().all())
        ok(len(locked) == 1, "A 先锁住 1 条", str([m.id for m in locked]))

        got = inbox.claim(d2, key, limit=5, lease_s=30)
        ids = [m.id for m in got]
        ok(locked and locked[0].id not in ids,
           "B 领取时跳过 A 锁住的那条（SKIP LOCKED 生效）", f"locked={ids}")
        ok(len(got) >= 1, "B 仍然领到了别的消息，没有被阻塞", f"ids={ids}")
        d1.commit()      # 释放锁，把 A 那条也标成 working 才算一致
        for m in locked:
            m.status = "working"
            m.lease_owner = inbox.owner_of(key)
            m.lease_until = datetime.now() + timedelta(seconds=30)
        d1.commit()
        for m in got:
            inbox.finish(d2, m, ok=True)
        d2.commit()
    finally:
        d1.close()
        d2.close()


# ---------------------------------------------------------------------------
# [2] 租约到期可重领 + 毒消息判死
# ---------------------------------------------------------------------------
def check_lease(key: str) -> None:
    print("\n[2] 租约到期可重领；必炸的消息不会堵死收件箱")
    _clear_inbox(key)
    db = session_as()
    try:
        m = bus.send(db, from_agent="dispatch", to_agent=key, msg_type="notify",
                     payload={"event": "selftest-lease"},
                     thread_id="selftest:daemon", commit=True)
        mid = m.id
    finally:
        db.close()

    d1 = session_as()
    # 把领取时刻卡在 .5s：`lease_until` 是 DATETIME(0)，微秒会被四舍五入进位 ——
    # 进位时"1 秒租约"实际是 1.5 秒，紧接着的 1.3 秒等待就领不到，
    # 表现为本套件**偶发**红（一度以为是机器慢）。固定 now 之后这里不再靠运气。
    t0 = datetime.now().replace(microsecond=500000)
    got = inbox.claim(d1, key, limit=10, lease_s=1, now=t0)
    mine = [x for x in got if x.id == mid]
    ok(bool(mine), "第一次领取成功", str([x.id for x in got]))
    ok(mine and mine[0].status == "working",
       "领取后状态是 working（不是 consumed，能看出在谁手上）")
    ok(mine and mine[0].tries == 1, "tries 记了一次", str(mine[0].tries if mine else None))
    ok(mine and mine[0].lease_until == t0.replace(microsecond=0) + timedelta(seconds=1),
       "租约长度 = 申请的秒数（不被 DATETIME 进位拉长）",
       str(mine[0].lease_until if mine else None))
    d1.close()                       # 不 ack，模拟进程被杀

    time.sleep(1.3)
    d2 = session_as()
    again = inbox.claim(d2, key, limit=10, lease_s=30)
    ids = [x.id for x in again]
    ok(mid in ids, "租约过期后被重新领到（消息没丢）", str(ids))
    row = d2.get(AgentMessage, mid)
    ok(int(row.tries) == 2, "tries 累加到 2", str(row.tries))

    # 毒消息：超过上限
    row.tries = inbox.MAX_TRIES
    d2.commit()
    inbox.finish(d2, row, ok=False, error="boom")
    d2.commit()
    d2.expire_all()
    ok(d2.get(AgentMessage, mid).status == "failed",
       f"tries 达到上限 {inbox.MAX_TRIES} 后判 failed", "")
    nxt = inbox.claim(d2, key, limit=10, lease_s=30)
    ok(mid not in [x.id for x in nxt], "判死之后不再被领取（收件箱不会被堵）", "")
    for x in nxt:
        inbox.finish(d2, x, ok=True)
    d2.commit()
    d2.close()


# ---------------------------------------------------------------------------
# [3] await_reply 跨连接可见
# ---------------------------------------------------------------------------
def check_await(key: str, order_id: int) -> None:
    print("\n[3] 长轮询：能等到另一条连接提交的回复")
    db = session_as()
    try:
        m = bus.send(db, from_agent=f"carrier:{ORG_A}", to_agent="dispatch",
                     msg_type="request", payload={"fields": ["origin"]},
                     order_id=order_id, commit=True)
        mid = m.id
    finally:
        db.close()

    t0 = time.monotonic()

    def _work() -> None:
        time.sleep(0.4)
        d = session_as()
        try:
            for msg in inbox.claim(d, "dispatch", limit=5, lease_s=30):
                dispatch_daemon.HANDLERS[msg.msg_type](d, msg)
                d.commit()
                inbox.finish(d, msg, ok=True)
                d.commit()
        finally:
            d.close()

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    reply = bus.await_reply(mid, timeout_s=6.0, poll_ms=50)
    took = time.monotonic() - t0
    th.join(timeout=3)

    ok(reply is not None, "等到回复（没有一直读到旧快照）", str(reply))
    ok(reply and reply.get("type") == "notify",
       "回复类型 notify（字段返回）", str(reply))
    ok(reply and (reply.get("payload") or {}).get("event") == "字段返回",
       "回复载荷是真正的查询结果", str(reply.get("payload") if reply else ""))
    ok(0.3 <= took < 5.5, f"耗时 {took:.2f}s 在合理区间（长轮询不是死等）")

    t1 = time.monotonic()
    none = bus.await_reply(999_999_999, timeout_s=0.4, poll_ms=50)
    ok(none is None, "没有回复时超时返回 None，不抛异常")
    ok(time.monotonic() - t1 < 1.5, "超时按 timeout_s 收口，不会无限等", "")


# ---------------------------------------------------------------------------
# [4] 四个 daemon 各自处理自己的收件箱
# ---------------------------------------------------------------------------
def check_four_daemons(order_id: int) -> None:
    print("\n[4] 四个 daemon 各管各的收件箱")
    db = session_as()
    try:
        # 异常域：承运商报故障
        bus.send(db, from_agent=f"carrier:{ORG_A}", to_agent="exception",
                 msg_type="propose",
                 payload={"amount": None, "reason": "制冷机故障，厢温升到 9.5 度",
                          "description": "制冷机故障，厢温升到 9.5 度"},
                 order_id=order_id, commit=True)
        # 承运商：调度问报价口径
        bus.send(db, from_agent="dispatch", to_agent=f"carrier:{ORG_A}",
                 msg_type="request", payload={"fields": ["quoted_price"]},
                 order_id=order_id, commit=True)
        # 货主：调度提交提价建议
        bus.send(db, from_agent="dispatch", to_agent="shipper",
                 msg_type="escalate",
                 payload={"reason": "承运商要价高于可付上限", "ask": "是否提价",
                          "gap": 406.0}, order_id=order_id, commit=True)
    finally:
        db.close()

    for mod, keys in ((exception_daemon, ["exception"]),
                      (shipper_daemon, ["shipper"]),
                      (carrier_daemon, [f"carrier:{ORG_A}"]),
                      (dispatch_daemon, ["dispatch"])):
        d = session_as()
        try:
            stats = {"handled": 0}
            for k in keys:
                for msg in inbox.claim(d, k, limit=10, lease_s=30):
                    r = mod.HANDLERS[msg.msg_type](d, msg)
                    d.commit()
                    inbox.finish(d, msg, ok=True)
                    d.commit()
                    if r is not None:
                        stats["handled"] += 1
            ok(stats["handled"] >= 1, f"{mod.KIND} daemon 处理了自己的消息",
               str(stats))
        finally:
            d.close()

    db = session_as()
    try:
        rows = bus.thread(db, f"order:{order_id}")
        pairs = {(r["from"], r["to"], r["type"]) for r in rows}
        ok(("exception", "dispatch", "propose") in pairs,
           "异常域把定级结论发给调度（不是直接改单）", str(sorted(pairs)))
        ok(any(p[0] == f"carrier:{ORG_A}" and p[2] == "notify" for p in pairs)
           or any(p[1] == f"carrier:{ORG_A}" and p[2] == "notify" for p in pairs),
           "承运商回了报价口径", str(sorted(pairs)))
        ok(("shipper", "dispatch", "notify") in pairs,
           "货主回执给调度（说明待本人确认）", str(sorted(pairs)))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# [5] due() 闭环
# ---------------------------------------------------------------------------
def check_due(db, order) -> None:
    print("\n[5] 到期检查：到点裁决 → 进协商 → 立刻还价")
    r = tender_ops.open_tender(db, order, U_DISPATCH, minutes=30, commit=True)
    tid = int(r["tender_id"])
    # 承运商在截止前报一个超预算 10% 的价（4900 * 1.102 ≈ 5400）
    br = tender_ops.submit_bid(db, {"user_id": 3, "account": "carrier1",
                                    "role": "carrier", "org_id": ORG_A},
                               order.id, quote_amount=5400, eta_h=24, commit=True)
    ok(br.get("order_status") == "published", "投标不改变订单状态（bug 回归）",
       str(br.get("order_status")))

    t = db.get(Tender, tid)
    t.deadline_at = datetime.now() - timedelta(minutes=1)
    db.commit()

    out = dispatch_daemon.due(db, ["dispatch"])
    db.commit()
    db.expire_all()
    t = db.get(Tender, tid)
    ok(t.status == "negotiating", "截止后自动进入协商", f"status={t.status} out={out}")
    ok("closed" in out, "due() 报告了这轮裁决数", str(out))

    db2 = session_as()
    try:
        counters = db2.execute(select(AgentMessage).where(
            AgentMessage.thread_id == f"order:{order.id}",
            AgentMessage.msg_type == "counter")).scalars().all()
        ok(bool(counters), "进入协商后立刻发出了 counter（不是只改状态）",
           str([(c.from_agent, c.to_agent) for c in counters]))
        c0 = counters[0] if counters else None
        ok(c0 is not None and c0.to_agent == f"carrier:{ORG_A}",
           "还价发给了投标的那家", c0.to_agent if c0 else "")
        ok(c0 is not None and abs(float((c0.payload or {}).get("amount") or 0)
                                  - 4900.0) < 0.01,
           "还价金额 = 招标上限（调度没有加价权）",
           str((c0.payload or {}).get("amount") if c0 else ""))
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# [6] 故障域：真进程，杀 dispatch
# ---------------------------------------------------------------------------
def _spawn(kind: str, *extra: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "sca.platform.agents.worker", "--kind", kind,
           "--no-echo", "--log-level", "INFO", *extra]
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _wait_alive(keys: list[str], timeout_s: float = 15.0) -> bool:
    """等心跳出现。**每次轮询换一个 Session**——MySQL 默认 REPEATABLE READ，
    同一个 Session 反复查只会看到第一次的快照，心跳明明写进去了也读不到。
    这不是测试的技巧，是 bus.await_reply 里同一个坑。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        d = session_as()
        try:
            if all(bus.alive(d, k, stale_s=60) for k in keys):
                return True
        finally:
            d.close()
        time.sleep(0.2)
    return False


def check_fault_domain(order_id: int) -> None:
    print("\n[6] 故障域：kill dispatch，carrier 继续干活")
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    dl = logdir / "agent_dispatch.log"
    cl = logdir / "agent_carrier.log"

    pd = _spawn("dispatch", "--poll-ms", "120")
    pc = _spawn("carrier", "--org", str(ORG_B), "--poll-ms", "120")
    try:
        ok(_wait_alive(["dispatch", f"carrier:{ORG_B}"], 20),
           "两个 agent 都起来了（心跳可见）",
           f"dispatch_pid={pd.pid} carrier_pid={pc.pid}")

        pd.terminate()
        pd.wait(timeout=10)
        ok(pd.poll() is not None, "dispatch 进程已被杀掉", str(pd.poll()))

        d_before = dl.stat().st_size if dl.exists() else 0
        c_before = cl.stat().st_size if cl.exists() else 0
        time.sleep(1.2)                       # 让被杀进程的日志彻底停下来

        db = session_as()
        try:
            m = bus.send(db, from_agent="dispatch", to_agent=f"carrier:{ORG_B}",
                         msg_type="request", payload={"fields": ["quoted_price"]},
                         order_id=order_id, commit=True)
            mid = m.id
        finally:
            db.close()

        status = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 12:
            d2 = session_as()
            try:
                row = d2.get(AgentMessage, mid)
                status = row.status if row else None
                if status in ("ack", "failed"):
                    break
            finally:
                d2.close()
            time.sleep(0.15)

        took = time.monotonic() - t0
        ok(status == "ack", f"dispatch 死了，carrier 仍然处理了消息（{took:.1f}s）",
           f"status={status}")
        ok(took < 5.0, f"处理延迟 {took:.2f}s < 5s（门铃 + 短轮询）")

        time.sleep(1.0)
        c_after = cl.stat().st_size if cl.exists() else 0
        d_after = dl.stat().st_size if dl.exists() else 0
        ok(c_after > c_before, "carrier 日志继续增长（它还活着）",
           f"{c_before} -> {c_after}")
        ok(d_after == d_before, "dispatch 日志停住了（它确实死了）",
           f"{d_before} -> {d_after}")
    finally:
        for p in (pd, pc):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()


# ---------------------------------------------------------------------------
# [8] 目标函数真的进决策
# ---------------------------------------------------------------------------
def _set_utility(db, key: str, utility: dict) -> None:
    from sca.platform.models import AgentActor

    row = db.execute(select(AgentActor).where(
        AgentActor.agent_key == key)).scalars().first()
    if row is None:
        db.add(AgentActor(agent_key=key, kind=key.split(":", 1)[0], utility=utility))
    else:
        row.utility = dict(utility)
    db.commit()


def check_utility_drives_decisions(db) -> None:
    """第 3 条检验的核心：改 agent 的目标，决策结果必须跟着变。

    如果改了 utility 结果不变，那 `agent_actor.utility` 就是一张装饰表，
    "四个 agent 目标不同"只是文档里的说法——这正是改造前
    `order_plan.arbitration` 一直空着的同一类问题。
    """
    print("\n[8] 目标函数进决策：改 utility，价格与裁决结果都变")

    u_carrier = {"profit": 0.0, "utilization": 0.3, "risk": 0.1}
    u_dispatch = {"margin": 0.35, "fulfilment": 0.4, "risk": 0.25}
    try:
        # (a) 承运商：更想赚钱 → 报得更高
        o2 = _mk_order(db, "2")
        tender_ops.open_tender(db, o2, U_DISPATCH, minutes=30, commit=True)
        _set_utility(db, f"carrier:{ORG_A}", {"profit": 0.0, "utilization": 0.3,
                                              "risk": 0.1})
        r_low = tender_ops.submit_bid(
            db, {"user_id": 3, "org_id": ORG_A, "role": "carrier"},
            o2.id, commit=True)

        _set_utility(db, f"carrier:{ORG_A}", {"profit": 1.0, "utilization": 0.3,
                                              "risk": 0.1})
        r_high = tender_ops.submit_bid(
            db, {"user_id": 3, "org_id": ORG_A, "role": "carrier"},
            o2.id, commit=True)

        ok(r_high["quote"] > r_low["quote"],
           "承运商 profit 权重越高，报价越高（自己的立场进了报价）",
           f"{r_low['quote']} -> {r_high['quote']}")
        ok(abs(r_low["floor"] - r_high["floor"]) < 0.01,
           "两次成本的底线没变，变的只是它愿意赚多少",
           f"{r_low['floor']} vs {r_high['floor']}")

        # (b) 调度：更看重履约 → 愿意多谈，超预算 20% 的单不直接判死
        def _mk_and_bid(suffix: str, quote: float):
            o = _mk_order(db, suffix)
            tender_ops.open_tender(db, o, U_DISPATCH, minutes=30, commit=True)
            tender_ops.submit_bid(
                db, {"user_id": 3, "org_id": ORG_A, "role": "carrier"},
                o.id, quote_amount=quote, eta_h=24, commit=True)
            return o

        # 预算 = 5000 × 0.98 = 4900；报 5880 = 超 20%
        o3 = _mk_and_bid("3", 5880)
        _set_utility(db, "dispatch", {"margin": 0.35, "fulfilment": 0.0,
                                      "risk": 0.25})
        r3 = tender_ops.close_tender(db, o3, U_DISPATCH, force=True, commit=True)
        ok(r3["status"] == "failed",
           "调度只想着抽成（fulfilment=0）→ 超 20% 直接流标", str(r3.get("status")))

        o4 = _mk_and_bid("4", 5880)
        _set_utility(db, "dispatch", {"margin": 0.35, "fulfilment": 1.0,
                                      "risk": 0.25})
        r4 = tender_ops.close_tender(db, o4, U_DISPATCH, force=True, commit=True)
        ok(r4["status"] == "negotiating",
           "调度更看重履约（fulfilment=1）→ 同样的单拉进谈判",
           str(r4.get("status")))

        pol = (r3.get("arbitration") or {}).get("policy") or {}
        ok(abs(float(pol.get("negotiate_max_ratio") or 0) - 0.05) < 1e-6,
           "裁决证据记下了可谈上限（0.05），能回溯为什么判死", str(pol))
        pol4 = (r4.get("arbitration") or {}).get("policy") or {}
        ok(abs(float(pol4.get("negotiate_max_ratio") or 0) - 0.30) < 1e-6,
           "另一单记的是 0.30，同一套数据不同立场结果不同", str(pol4))
        ok(bool(pol4.get("weights")) and "fulfilment" in (pol4.get("dispatch_utility") or {}),
           "证据里同时留了平台权重与调度自己的目标", str(pol4))
    finally:
        _set_utility(db, f"carrier:{ORG_A}", u_carrier)
        _set_utility(db, "dispatch", u_dispatch)


# ---------------------------------------------------------------------------
# [7] 门铃
# ---------------------------------------------------------------------------
def check_wake(key: str) -> None:
    print("\n[7] Redis 门铃：发消息会叫醒对方；叫不醒也不影响正确性")
    try:
        wake.drain(key)
        ok(wake.ring(key), "按门铃成功（Redis 可用）")
        got = wake.wait([key], 1.0)
        ok(got == key, "等门铃的一方立刻被唤醒", str(got))
        t0 = time.monotonic()
        none = wake.wait([key], 0.3)
        ok(none is None and 0.25 <= time.monotonic() - t0 < 1.5,
           "没有门铃时按超时返回", f"{time.monotonic() - t0:.2f}s")
    except Exception as exc:            # noqa: BLE001
        ok(False, "门铃需要 Redis", f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-process", action="store_true",
                    help="跳过真进程故障域测试（不需要停 start_agents.bat）")
    args = ap.parse_args()

    print("=" * 66)
    print("S4 四进程常驻验证")
    print("=" * 66)

    db = session_as()
    try:
        _cleanup(db)
        ok(True, "上一轮测试数据已清理")
        _mk_carrier(db, ORG_A, "S4 测试承运商 A")
        _mk_carrier(db, ORG_B, "S4 测试承运商 B")
        order = _mk_order(db)
        db.commit()
        oid = order.id
        ok(oid > 0, f"测试订单已建（id={oid}）")
    finally:
        db.close()

    try:
        check_skip_locked(f"carrier:{ORG_A}")
        check_lease(f"carrier:{ORG_A}")
        check_await(f"carrier:{ORG_A}", oid)
        check_four_daemons(oid)

        db = session_as()
        try:
            order = db.get(ShipperOrder, oid)
            check_due(db, order)
            check_utility_drives_decisions(db)
        finally:
            db.close()

        check_wake(f"carrier:{ORG_A}")

        if args.skip_process:
            print("\n[6] 故障域：已跳过（--skip-process）")
        else:
            check_fault_domain(oid)
    finally:
        db = session_as()
        try:
            _cleanup(db)
            print("\n  （测试数据已清理）")
        finally:
            db.close()

    print(f"\n=== {PASSED} passed, {len(FAILS)} failed ===")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
