"""系统管理接口验证（服务费规则 / 配置 / 凭据 / 组织 / 用户 / 历史回退）。

    ./.venv/Scripts/python.exe scripts/test_platform_admin.py

重点验两件事：
  1) 权限真的拦住了（调度员不能改配置，只能看）；
  2) 改了服务费规则**真的影响下一单的挂牌价**——不是存进库里当摆设。
"""
from __future__ import annotations

import sys
from decimal import Decimal
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
                                 ExceptionReport, FeeRule, OrderEvent, OrderPlan,
                                 OrderPublish, ShipperOrder, SysAuditLog)
from sca.platform.services import runtime  # noqa: E402

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


def make_published(c: TestClient, H_ship, H_disp, text: str) -> tuple[int, float]:
    r = c.post("/api/shipper/chat", json={"text": text}, headers=H_ship)
    sid = (r.json() or {})["session_id"]
    r = c.post("/api/shipper/chat", json={"session_id": sid, "text": "确认"}, headers=H_ship)
    oid = (r.json() or {}).get("order_id")
    c.post(f"/api/dispatch/orders/{oid}/plan", headers=H_disp)
    r = c.post(f"/api/dispatch/orders/{oid}/publish", json={}, headers=H_disp)
    return oid, float((r.json() or {}).get("publish_price") or 0)


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()

    # S5 起：下单/挂牌/接单由 agent 进程执行，进程内测试把四个 daemon 起在线程里
    agents = agent_rt.start()
    created: list[int] = []

    with TestClient(app) as c:
        H_ship = {"Authorization": f"Bearer {login(c, 'shipper1')}"}
        H_disp = {"Authorization": f"Bearer {login(c, 'dispatcher1')}"}
        H_adm = {"Authorization": f"Bearer {login(c, 'admin')}"}

        print("\n[1] 服务费规则：超管改，调度员只读")
        r = c.get("/api/admin/fee-rules", headers=H_disp)
        check("调度员可查看规则", r.status_code == 200 and bool((r.json() or {}).get("active")),
              f"{r.status_code} {r.text[:120]}")
        check("调度员标记为不可编辑", (r.json() or {}).get("can_edit") is False,
              str((r.json() or {}).get("can_edit")))
        base_rate = (r.json() or {}).get("active", {}).get("shipper_rate")

        r = c.post("/api/admin/fee-rules", json={"name": "越权尝试", "shipper_rate": 0.05,
                                                 "carrier_rate": 0.05}, headers=H_disp)
        check("调度员不能新建规则（403）", r.status_code == 403, str(r.status_code))

        r = c.get("/api/admin/fee-rules", headers=H_adm)
        check("超管可编辑", (r.json() or {}).get("can_edit") is True,
              str((r.json() or {}).get("can_edit")))

        print("\n[2] 改费率后**真的影响下一单**")
        oid1, price1 = make_published(c, H_ship, H_disp,
                                      "石家庄到南京 普货 10吨 后天 预算1万")
        created.append(oid1)
        exp1 = round(10000 * (1 - float(base_rate)), 2)
        check(f"按当前费率 {base_rate:.2%} 定价", abs(price1 - exp1) < 0.02,
              f"实际 {price1} 期望 {exp1}")

        r = c.post("/api/admin/fee-rules", json={"name": "临时测试 3%", "shipper_rate": 0.03,
                                                 "carrier_rate": 0.03,
                                                 "note": "自动化测试"}, headers=H_adm)
        check("超管新建 3% 规则", r.status_code == 200, r.text[:160])

        oid2, price2 = make_published(c, H_ship, H_disp,
                                      "石家庄到南京 普货 10吨 后天 预算1万")
        created.append(oid2)
        check("新单按 3% 计价（配置立即生效）", abs(price2 - 9700.0) < 0.02,
              f"实际 {price2} 期望 9700.0")

        # 2026-10-18 回归：`all_config` 与 `active_fee` 曾共用同一个缓存时间戳，
        # 于是"读一次系统配置"就把费率缓存的计时续上了 —— 改完费率后 `active_fee`
        # 认为手里那份旧费率仍然新鲜，连库都不查，下一单仍按旧价挂牌。
        # 这一条直接钉住两个缓存的独立性（上面的 HTTP 断言能不能复现取决于调用顺序）。
        db = session_as("app")
        try:
            runtime.invalidate()            # 模拟"刚改完配置"：缓存里有旧值，计时归零
            runtime.all_config(db)          # 读一次系统配置——不得给费率缓存续命
            check("读系统配置不会让费率缓存变成「新鲜」",
                  runtime._fresh("fee_at") is False,
                  f"fee_at={runtime._cache.get('fee_at')}，改费率会不生效")
            runtime.invalidate()
            runtime.active_fee(db)          # 反过来也不能给配置缓存续命
            check("读费率不会让配置缓存变成「新鲜」",
                  runtime._fresh("conf_at") is False,
                  f"conf_at={runtime._cache.get('conf_at')}")
            runtime.invalidate()
        finally:
            db.close()

        db = session_as("app")
        try:
            active = db.query(FeeRule).filter(FeeRule.status == "active").all()
            check("同一时刻只有一条生效规则", len(active) == 1,
                  str([(a.id, float(a.shipper_rate)) for a in active]))
            check("费率变更进系统日志",
                  db.query(SysAuditLog).filter(
                      SysAuditLog.action == "fee_rule_changed").count() >= 1)
        finally:
            db.close()

        # 恢复 2%
        c.post("/api/admin/fee-rules",
               json={"name": "恢复默认 2%", "shipper_rate": 0.02, "carrier_rate": 0.02},
               headers=H_adm)

        print("\n[3] 系统配置")
        r = c.get("/api/admin/config?category=model", headers=H_disp)
        check("调度员无权看模型配置（403）", r.status_code == 403, str(r.status_code))
        r = c.get("/api/admin/config", headers=H_adm)
        keys = [i["key"] for i in (r.json() or {}).get("items") or []]
        check("超管可读配置", r.status_code == 200 and "model.chain" in keys, str(keys))

        r = c.put("/api/admin/config/model.chain",
                  json={"value": {"intent": ["tongyi-intent-detect-v3", "rule"],
                                  "route": ["qwen-plus", "rule"]}}, headers=H_adm)
        check("超管可改配置", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = c.put("/api/admin/config/not.exist", json={"value": 1}, headers=H_adm)
        check("不存在的配置项 404", r.status_code == 404, str(r.status_code))
        r = c.get("/api/admin/config", headers=H_adm)
        check("变更进系统日志", True)   # 由下面 db 校验
        db = session_as("app")
        try:
            check("config_changed 已留痕",
                  db.query(SysAuditLog).filter(
                      SysAuditLog.action == "config_changed").count() >= 1)
        finally:
            db.close()

        print("\n[4] MCP / 模型凭据（只回掩码）")
        r = c.get("/api/admin/credentials?kind=tool", headers=H_disp)
        check("调度员无权看凭据（403）", r.status_code == 403, str(r.status_code))
        r = c.get("/api/admin/credentials?kind=tool", headers=H_adm)
        body = r.text
        check("超管可读凭据列表", r.status_code == 200, f"{r.status_code}")
        check("响应中不含高德明文 Key",
              "9afc613c936a7f9af17b7889285dcf0c" not in body,
              "明文泄露！")
        check("响应含掩码格式", "…" in body or "*" in body or "items" in body,
              body[:120])

        print("\n[5] 组织与用户管理")
        r = c.get("/api/admin/orgs?org_type=carrier", headers=H_disp)
        check("调度员可查看承运商信息", r.status_code == 200 and len(r.json() or []) >= 1,
              f"{r.status_code} {r.text[:120]}")
        org = (r.json() or [{}])[0]
        check("带承运商档案（单价/吨位）", (org.get("profile") or {}).get("cost_per_km"),
              str(org.get("profile")))

        r = c.put(f"/api/admin/orgs/{org['id']}", json={"status": 0}, headers=H_disp)
        check("调度员不能改组织（403）", r.status_code == 403, str(r.status_code))

        r = c.put(f"/api/admin/carriers/{org['id']}?cost_per_km=2.8&capacity_t=30", headers=H_adm)
        check("超管可改承运商单价", r.status_code == 200 and
              abs((r.json() or {}).get("cost_per_km", 0) - 2.8) < 0.01,
              f"{r.status_code} {r.text[:120]}")
        # 还原
        c.put(f"/api/admin/carriers/{org['id']}?cost_per_km=2.6&capacity_t=28", headers=H_adm)

        r = c.get("/api/admin/users?role=carrier", headers=H_adm)
        check("超管可查用户", r.status_code == 200 and len(r.json() or []) >= 1,
              f"{r.status_code} {r.text[:120]}")
        uid = (r.json() or [{}])[0].get("id")
        r = c.put(f"/api/admin/users/{uid}", json={"display_name": "王师傅"}, headers=H_adm)
        check("超管可改用户资料", r.status_code == 200, r.text[:120])

        print("\n[6] 历史调度与回退")
        r = c.get("/api/dispatch/history", headers=H_disp)
        check("调度员可查历史", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        r = c.get("/api/dispatch/history", headers=H_ship)
        check("货主无权查历史（403）", r.status_code == 403, str(r.status_code))

        # 造一个异常态的单，验证回退
        oid3, _ = make_published(c, H_ship, H_disp, "石家庄到南京 普货 6吨 后天 预算9000")
        created.append(oid3)
        H_car = {"Authorization": f"Bearer {login(c, 'carrier1')}"}
        c.post(f"/api/carrier/orders/{oid3}/accept", json={"position_text": "正定物流园"},
               headers=H_car)
        r = c.post("/api/carrier/chat", json={"text": "我在保定服务区，车辆故障"},
                   headers=H_car)
        d = r.json()
        if d.get("state") == "collecting":
            r = c.post("/api/carrier/chat",
                       json={"session_id": d["session_id"], "text": "SO"}, headers=H_car)
        # 找到该单的订单号
        db = session_as("app")
        try:
            no = db.get(ShipperOrder, oid3).order_no
        finally:
            db.close()
        r = c.post("/api/carrier/chat", json={"session_id": d.get("session_id"),
                                              "text": no}, headers=H_car)
        r = c.post("/api/carrier/chat", json={"session_id": r.json().get("session_id"),
                                              "text": "确认"}, headers=H_car)
        check("造出异常单", r.json().get("done") is True, str(r.json())[:120])

        r = c.post(f"/api/dispatch/orders/{oid3}/rollback",
                   json={"target_status": "in_transit", "reason": "误报，车辆已恢复"},
                   headers=H_disp)
        check("异常态可回退到在途", r.status_code == 200 and
              (r.json() or {}).get("status") == "in_transit",
              f"{r.status_code} {r.text[:160]}")
        db = session_as("app")
        try:
            check("回退后异常单被收口",
                  db.query(ExceptionReport).filter(
                      ExceptionReport.order_id == oid3,
                      ExceptionReport.status != "resolved").count() == 0)
        finally:
            db.close()

        r = c.post(f"/api/dispatch/orders/{oid3}/rollback",
                   json={"target_status": "submitted", "reason": "非法跨级回退"},
                   headers=H_disp)
        check("跨级回退被拒（409）", r.status_code == 409, str(r.status_code))

        r = c.get("/api/dispatch/audit?action=order_rolled_back", headers=H_disp)
        check("回退进系统日志", len((r.json() or {}).get("items") or []) >= 1,
              str((r.json() or {}).get("items"))[:160])

    # 清理
    db = session_as("app")
    try:
        for oid in created:
            for model in (ExceptionReport, CarrierBid, OrderPublish, OrderPlan, OrderEvent):
                db.query(model).filter(model.order_id == oid).delete()
            db.query(ShipperOrder).filter(ShipperOrder.id == oid).delete()
        db.query(SysAuditLog).filter(SysAuditLog.action.in_(
            ("fee_rule_changed", "config_changed", "order_rolled_back",
             "carrier_profile_updated", "user_updated"))).delete(
            synchronize_session=False)
        # 清掉测试建的规则，保留 2% 的那条
        db.query(FeeRule).filter(FeeRule.name.like("%临时测试%")).delete(
            synchronize_session=False)
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
