"""工具权限闸门与审计分域（S1）。

    ./.venv/Scripts/python.exe scripts/test_agent_permissions.py

要证明三件事：

1. 每个角色的服务白名单是**有实质排除**的，不是四个一样的集合
   （承运商不能拿 reassign，异常域不能碰 pricing，货主不能替承运商接单）；
2. ``check_isolation.py`` 真的能抓到越权 import —— 用一个临时文件造违规，
   而不是只看它返回 0；
3. 审计日志能记下 ``agent_key``，人工操作留空。

第 3 项需要数据库；前两项是纯函数与 AST，不依赖库。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.platform.agents import permissions as P  # noqa: E402
from sca.platform.services import audit  # noqa: E402

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


# ---------------------------------------------------------------------------
def check_whitelist() -> None:
    print("\n[白名单：每个角色的排除项都不同]")
    ok(set(P.ALLOWED_SERVICES) == set(P.KINDS),
       "四个角色都有白名单", str(sorted(P.ALLOWED_SERVICES)))

    # 承运商不能给自己改派，也不能碰定价全量（钱的口径由平台算）
    ok("reassign" not in P.allowed_for("carrier"),
       "承运商不得使用 reassign（不能给自己改派）")
    ok("pricing" not in P.allowed_for("carrier"),
       "承运商不得直接使用 pricing")

    # 异常域的目标权重里 cost=0，它的视图和工具链里都不能出现钱
    ok("pricing" not in P.allowed_for("exception"),
       "异常域不得使用 pricing（定级不能被成本污染）")
    ok("carrier_ops" not in P.allowed_for("exception"),
       "异常域不得使用 carrier_ops")

    # 货主不能替承运商接单
    ok("carrier_ops" not in P.allowed_for("shipper"),
       "货主不得使用 carrier_ops（不能替承运商接单）")

    # 调度是裁决者，但成交仍要承运商自己执行；accept 在 carrier_ops 里，
    # 调度没有该服务 = 结构上无法替承运商签字
    ok("carrier_ops" not in P.allowed_for("dispatch"),
       "调度不得使用 carrier_ops（成交必须由承运商 agent 执行）")

    # 调度必须能裁决与改派
    ok({"triage", "reroute", "reassign", "tender"} <= P.allowed_for("dispatch"),
       "调度拥有裁决所需的全部服务")


def check_checker() -> None:
    print("\n[纯函数：check 给出的是越权项，不是布尔笑话]")
    good, bad = P.check("carrier", ["carrier_ops", "notify"])
    ok(good and bad == [], "合规 import 通过", str(bad))

    good, bad = P.check("carrier", ["carrier_ops", "reassign"])
    ok(not good and bad == ["reassign"], "越权项被准确列出", str(bad))

    msg = P.explain("carrier", ["reassign"])
    ok("reassign" in msg and "可用服务" in msg,
       "违规说明包含原因与可用范围，不是一句不允许", msg)

    ok(P.kind_of("carrier_daemon.py") == "carrier", "daemon 文件能识别角色")
    ok(P.kind_of("exception_agent.py") == "exception", "agent 文件能识别角色")
    ok(P.kind_of("pump.py") is None, "非 agent/daemon 文件不套白名单")


def check_static_gate() -> None:
    print("\n[静态闸门：真的能抓到违规，而不是只会返回 0]")
    import check_isolation as CI

    # 造一个越权文件。check_file 只看文件名与内容，放在临时目录即可。
    tmpdir = Path(tempfile.mkdtemp())
    probe = tmpdir / "carrier_agent.py"
    probe.write_text("from ..services import reassign\n", encoding="utf-8")
    problems = CI.check_file(probe)
    ok(any("越权 import 服务" in p and "reassign" in p for p in problems),
       "承运商 import reassign 被抓到", str(problems))

    probe2 = tmpdir / "exception_agent.py"
    probe2.write_text("from ..services import pricing\n", encoding="utf-8")
    problems2 = CI.check_file(probe2)
    ok(any("pricing" in p for p in problems2),
       "异常域 import pricing 被抓到", str(problems2))

    probe3 = tmpdir / "shipper_agent.py"
    probe3.write_text("from ..services import carrier_ops\n", encoding="utf-8")
    problems3 = CI.check_file(probe3)
    ok(any("carrier_ops" in p for p in problems3),
       "货主 import carrier_ops 被抓到", str(problems3))

    # 合规文件不能误报
    probe4 = tmpdir / "carrier_agent.py"
    probe4.write_text("from ..services import carrier_ops, notify\n", encoding="utf-8")
    ok(CI.check_file(probe4) == [], "合规文件不误报", str(CI.check_file(probe4)))

    # 第 5 条：import 合规、但调了越权函数。承运商现在可以 import tender_ops
    # 去投标，必须在同一模块里把"给自己定标"这条路堵住。
    probe5 = tmpdir / "carrier_daemon.py"
    probe5.write_text(
        "from ..services import tender_ops\n"
        "def tick(db, order, user):\n"
        "    return tender_ops.close_tender(db, order, user, force=True)\n",
        encoding="utf-8")
    problems5 = CI.check_file(probe5)
    ok(any("越权调用 close_tender" in p for p in problems5),
       "承运商调 close_tender（给自己定标）被抓到", str(problems5))

    probe6 = tmpdir / "carrier_daemon.py"
    probe6.write_text(
        "from ..services import tender_ops\n"
        "def tick(db, user, oid):\n"
        "    return tender_ops.submit_bid(db, user, oid, quote_amount=4800)\n",
        encoding="utf-8")
    ok(CI.check_file(probe6) == [],
       "承运商调 submit_bid（投标）不算越权", str(CI.check_file(probe6)))

    # S5 起接单的执行体叫 accept_core（HTTP/对话都只投消息）。
    # 名字改了，禁令必须跟着改——否则"不能代签"只约束了旧名字。
    probe7 = tmpdir / "dispatch_agent.py"
    probe7.write_text(
        "from ..services import carrier_ops\n"
        "def _execute(db, user, oid):\n"
        "    return carrier_ops.accept_core(db, user, oid)\n",
        encoding="utf-8")
    problems7 = CI.check_file(probe7)
    ok(any("越权调用 accept_core" in p for p in problems7),
       "调度替承运商接单被抓到（不能代签）", str(problems7))

    # 真实的四个 Agent 文件应当全部通过
    real = sorted(list(CI.AGENTS_DIR.glob("*_agent.py")))
    allp: list[str] = []
    for p in real:
        allp.extend(CI.check_file(p))
    ok(not allp, f"现有 {len(real)} 个 Agent 文件全部合规", str(allp))

    # 第 6 条：路由层直接执行成交类写操作 = 绕过 Agent
    rdir = tmpdir / "routers"
    rdir.mkdir()
    (rdir / "bad_router.py").write_text(
        "from ..services import carrier_ops\n"
        "def accept(oid, user, db):\n"
        "    return carrier_ops.accept_core(db, user, oid, 'x')\n",
        encoding="utf-8")
    rp = CI.check_routes(rdir)
    ok(any("accept_core" in x for x in rp),
       "路由直接接单被抓到（成交必须交给 Agent）", str(rp))
    (rdir / "ok_router.py").write_text(
        "from . import _gateway as gw\n"
        "def accept(oid, user, db):\n"
        "    return gw.unwrap(gw.invoke(db, user, action='accept_order'))\n",
        encoding="utf-8")
    ok(CI.check_routes(rdir) and len(CI.check_routes(rdir)) == 1,
       "只投消息的路由不会被误报", str(CI.check_routes(rdir)))
    ok(not CI.check_routes(), "现有路由层全部合规（无人直接成交）",
       str(CI.check_routes()))
    for f2 in rdir.iterdir():
        f2.unlink()
    rdir.rmdir()

    for f in tmpdir.iterdir():
        f.unlink()
    tmpdir.rmdir()


def check_daemon_gate() -> None:
    """S4：daemon 是同一角色的另一个入口，闸门必须同样生效。

    这里最容易出现"换了入口就换了权限"的口子——daemon 单独一份文件，
    如果闸门只扫 `*_agent.py`，那么"不能看 budget_max"就只约束了对话入口，
    常驻进程想读什么读什么。
    """
    print("\n[S4 闸门：daemon 与 agent 同一套权限]")
    import check_isolation as CI

    tmpdir = Path(tempfile.mkdtemp())

    p1 = tmpdir / "carrier_daemon.py"
    p1.write_text(
        "def quote(db, order):\n"
        "    return order.budget_max * 0.98\n",
        encoding="utf-8")
    probs = CI.check_file(p1)
    ok(any("越权字段 budget_max" in x for x in probs),
       "承运商 daemon 读 budget_max 被抓到", str(probs))

    p2 = tmpdir / "carrier_daemon.py"
    p2.write_text(
        "from ..services import reassign\n"
        "def tick(db):\n"
        "    return reassign.plan(db)\n",
        encoding="utf-8")
    ok(any("reassign" in x for x in CI.check_file(p2)),
       "承运商 daemon import reassign 被抓到", str(CI.check_file(p2)))

    p3 = tmpdir / "exception_daemon.py"
    p3.write_text("from ..exception_agent import agent\n", encoding="utf-8")
    ok(CI.check_file(p3) == [],
       "同一角色（exception_daemon 引用 exception_agent）不算跨 Agent",
       str(CI.check_file(p3)))

    p4 = tmpdir / "carrier_daemon.py"
    p4.write_text("from ..dispatch_agent import agent\n", encoding="utf-8")
    ok(any("直接 import 另一个 Agent" in x for x in CI.check_file(p4)),
       "承运商 daemon 引用调度 agent 被抓到", str(CI.check_file(p4)))

    real = sorted(CI.AGENTS_DIR.glob("daemons/*_daemon.py"))
    allp: list[str] = []
    for p in real:
        allp.extend(CI.check_file(p))
    ok(len(real) == 4 and not allp,
       f"现有 {len(real)} 个 daemon 文件全部合规", str(allp))

    for f in tmpdir.iterdir():
        f.unlink()
    tmpdir.rmdir()


def check_audit() -> None:
    print("\n[审计分域：能看出是哪个 agent 做的]")
    try:
        from sqlalchemy import delete

        from sca.platform.db import session_as
        from sca.platform.models import SysAuditLog

        db = session_as("app")
        actor = {"user_id": 0, "account": "test:perm", "role": "dispatcher"}

        r1 = audit.log(db, actor, "test_agent_audit", agent_key="carrier:999")
        r2 = audit.log(db, actor, "test_agent_audit_human")
        db.commit()
        db.expire_all()

        g1 = db.get(SysAuditLog, r1.id)
        g2 = db.get(SysAuditLog, r2.id)
        ok(g1.agent_key == "carrier:999", "agent 写的审计带 agent_key", str(g1.agent_key))
        ok(g2.agent_key is None, "人工操作 agent_key 留空", str(g2.agent_key))

        rows = audit.query(db, action="test_agent_audit", limit=5)
        ok(any(r["agent_key"] == "carrier:999" for r in rows),
           "查询结果透出 agent_key", str(rows[:2]))

        db.execute(delete(SysAuditLog).where(
            SysAuditLog.action.like("test_agent_audit%")))
        db.commit()
        db.close()
    except Exception as exc:  # 数据库不可用时不静默跳过，明确失败
        ok(False, "审计分域（需要 MySQL）", f"{type(exc).__name__}: {exc}")


def main() -> int:
    check_whitelist()
    check_checker()
    check_static_gate()
    check_daemon_gate()
    check_audit()

    print(f"\n=== {PASSED} passed, {len(FAILS)} failed ===")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
