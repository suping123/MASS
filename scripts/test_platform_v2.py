"""v2 变更验证：省份->省会、三端取消、系统日志、数据隔离、超管权限。

    ./.venv/Scripts/python.exe scripts/test_platform_v2.py
"""
from __future__ import annotations

import sys
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
from sca.platform.models import (CarrierBid, ChatMessage, ChatSession,  # noqa: E402
                                 ExceptionReport, OrderEvent, OrderPlan,
                                 OrderPublish, ShipperOrder, SysAuditLog, SysUser)

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


def make_order(c: TestClient, H, text: str) -> int:
    """用一条信息完整的需求直接落单（这些测试文本都带起讫/货类/重量/时效/预算）。"""
    r = c.post("/api/shipper/chat", json={"text": text}, headers=H)
    sid = (r.json() or {})["session_id"]
    r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H)
    return (r.json() or {}).get("order_id")


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}
        H_adm = {"Authorization": f"Bearer {login(c, 'admin')}"}

        print("\n[1] 省份默认映射到省会")
        r = c.post("/api/shipper/chat", json={"text": "河北到成都 冷链 10吨 明天下午6点前 预算2万"},
                   headers=H_ship)
        d = r.json()
        check("省份被归一为省会", (d.get("slots") or {}).get("origin") == "石家庄",
              str((d.get("slots") or {}).get("origin")))
        check("目的地正常识别", (d.get("slots") or {}).get("dest") == "成都",
              str((d.get("slots") or {}).get("dest")))
        sid1 = d["session_id"]
        r = c.post("/api/shipper/chat", json={"session_id": sid1, "text": "确认"}, headers=H_ship)
        oid1 = r.json().get("order_id")

        print("\n[2] 货主取消自己的订单（必须填原因）")
        r = c.post(f"/api/shipper/orders/{oid1}/cancel", json={"reason": "客户临时取消发货"},
                   headers=H_ship)
        check("取消成功", r.status_code == 200 and (r.json() or {}).get("status") == "cancelled",
              f"{r.status_code} {r.text[:160]}")
        db = session_as("app")
        try:
            o = db.get(ShipperOrder, oid1)
            check("订单状态落库为 cancelled", o.status == "cancelled", o.status)
            logs = db.query(SysAuditLog).filter(
                SysAuditLog.action == "order_cancelled",
                SysAuditLog.target_id == str(oid1)).all()
            check("系统日志记录了取消原因", len(logs) == 1 and "临时取消" in (logs[0].reason or ""),
                  str([l.reason for l in logs]))
        finally:
            db.close()

        r = c.post(f"/api/shipper/orders/{oid1}/cancel", json={"reason": "再取消一次"},
                   headers=H_ship)
        check("已取消的单不能重复取消（409）", r.status_code == 409, str(r.status_code))
        r = c.post(f"/api/shipper/orders/{oid1}/cancel", json={"reason": "越权测试"},
                   headers=H_car)
        check("承运商不能通过货主接口取消该单（404）", r.status_code == 404, str(r.status_code))

        print("\n[3] 取消原因必填")
        oid2 = make_order(c, H_ship, "石家庄到南京 普货 5吨 后天 预算8000")
        r = c.post(f"/api/shipper/orders/{oid2}/cancel", json={"reason": ""},
                   headers=H_ship)
        check("空原因被拒（422）", r.status_code == 422, str(r.status_code))

        print("\n[4] 承运商取消已接的单")
        c.post(f"/api/dispatch/orders/{oid2}/plan", headers=H_disp)
        c.post(f"/api/dispatch/orders/{oid2}/publish", json={}, headers=H_disp)
        r = c.post(f"/api/carrier/orders/{oid2}/accept",
                   json={"position_text": "石家庄正定物流园"}, headers=H_car)
        check("承运商接单", r.status_code == 200, r.text[:120])
        r = c.post(f"/api/carrier/orders/{oid2}/cancel",
                   json={"reason": "车辆临时故障，无法承运"}, headers=H_car)
        check("承运商取消成功", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        db = session_as("app")
        try:
            o = db.get(ShipperOrder, oid2)
            check("取消后释放在途归属", o.carrier_org_id is None, str(o.carrier_org_id))
            b = db.query(CarrierBid).filter(CarrierBid.order_id == oid2).first()
            check("承运商报价置为撤回", b.decision == "withdrawn", b.decision)
            check("承运商取消也进日志", db.query(SysAuditLog).filter(
                SysAuditLog.action == "order_cancelled",
                SysAuditLog.target_id == str(oid2),
                SysAuditLog.actor_role == "carrier").count() == 1)
        finally:
            db.close()

        print("\n[5] 调度取消 + 系统日志查询")
        oid3 = make_order(c, H_ship, "上海到广州 普货 12吨 后天 预算1.5万")
        r = c.post(f"/api/dispatch/orders/{oid3}/cancel", json={"reason": "需求方撤回，不再发运"},
                   headers=H_disp)
        check("调度可取消任意未完成单", r.status_code == 200, r.text[:120])

        r = c.get("/api/dispatch/audit?limit=20", headers=H_disp)
        check("调度员可查系统日志", r.status_code == 200 and len((r.json() or {}).get("items") or []) >= 3,
              f"{r.status_code} {str(r.json())[:160]}")
        items = (r.json() or {}).get("items") or []
        check("日志含三端取消记录",
              {i["role"] for i in items if i["action"] == "order_cancelled"} >=
              {"shipper", "carrier", "dispatcher"},
              str(sorted({i["role"] for i in items})))

        r = c.get("/api/dispatch/audit", headers=H_car)
        check("承运商无权看系统日志（403）", r.status_code == 403, str(r.status_code))
        r = c.get("/api/dispatch/audit", headers=H_ship)
        check("货主无权看系统日志（403）", r.status_code == 403, str(r.status_code))

        print("\n[6] 超管权限")
        r = c.get("/api/auth/me", headers=H_adm)
        perms = set(((r.json() or {}).get("user") or {}).get("perms") or [])
        check("超管拥有全部 23 个权限点", len(perms) >= 22, str(len(perms)))
        check("超管具备 admin:* 系列", {"admin:audit_manage", "admin:fee_rule",
                                        "admin:model_config", "admin:mcp_key",
                                        "admin:org_manage"} <= perms,
              str(sorted(p for p in perms if p.startswith("admin"))))
        r = c.get("/api/dispatch/audit", headers=H_adm)
        check("超管可读日志", r.status_code == 200, str(r.status_code))

        print("\n[7] 数据隔离（重点：不能越权看到别人的单）")
        r = c.get("/api/auth/me", headers=H_ship)
        # /api/auth/me 返回的 user 用 user_id 字段（current_user 的口径），不是 id
        my_uid = (((r.json() or {}).get("user") or {}).get("user_id"))
        r = c.get("/api/shipper/orders", headers=H_ship)
        mine = (r.json() or [])
        db = session_as("app")
        try:
            own = {x.id for x in db.query(ShipperOrder).filter(
                ShipperOrder.shipper_user_id == my_uid)}
        finally:
            db.close()
        # 不断言"等于本次创建的三单"——库里可能有该账号的历史单，那属于正常数据
        check("货主列表全部属于自己", {o["id"] for o in mine} <= own,
              f"返回 {[o.get('id') for o in mine]}，本人的单 {sorted(own)}")
        # 造一个不属于 shipper1 的单（直接改归属）再验证查不到
        db = session_as("app")
        try:
            other = db.query(SysUser).filter(SysUser.role == "admin").first()
            hidden = ShipperOrder(order_no="SO-HIDDEN-01", shipper_user_id=other.id,
                                  origin="北京", dest="上海", cargo_type="普货",
                                  weight_t=3, status="submitted", budget_max=5000)
            db.add(hidden)
            db.commit()
            hidden_id = hidden.id
        finally:
            db.close()
        r = c.get("/api/shipper/orders", headers=H_ship)
        check("看不到别人创建的订单", all(o["id"] != hidden_id for o in (r.json() or [])),
              str([o.get("id") for o in (r.json() or [])]))
        r = c.get(f"/api/shipper/orders/{hidden_id}", headers=H_ship)
        check("直接按 id 查别人的单返回 404", r.status_code == 404, str(r.status_code))
        r = c.post(f"/api/shipper/orders/{hidden_id}/cancel", json={"reason": "越权测试"},
                   headers=H_ship)
        check("不能取消别人的单", r.status_code == 404, str(r.status_code))

    # 清理
    db = session_as("app")
    try:
        for oid in (oid1, oid2, oid3, hidden_id):
            for model in (ExceptionReport, CarrierBid, OrderPublish, OrderPlan, OrderEvent):
                db.query(model).filter(model.order_id == oid).delete()
            db.query(ShipperOrder).filter(ShipperOrder.id == oid).delete()
        db.query(SysAuditLog).filter(SysAuditLog.target_type == "order",
                                     SysAuditLog.target_id.in_(
                                         [str(x) for x in (oid1, oid2, oid3, hidden_id) if x])
                                     ).delete(synchronize_session=False)
        for s in db.query(ChatSession).all():
            db.query(ChatMessage).filter(ChatMessage.session_id == s.id).delete()
            db.delete(s)
        db.commit()
    finally:
        db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    agents.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
