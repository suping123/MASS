"""模型降级开关验证：改配置 -> 内核真的不再调模型。

    ./.venv/Scripts/python.exe scripts/test_platform_model_switch.py

为什么单独测：这类开关最容易做成"存进数据库的摆设"——
页面显示已关闭，内核照样调模型。所以这里直接调内核节点看它的决策。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from sca import runtime_flags  # noqa: E402
from sca.platform import security  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402
from sca.platform.models import SystemConfig, SysAuditLog  # noqa: E402

FAILS: list[str] = []
PASSED = 0

# 带路线偏好词的需求：默认会走模型增强选路
STATE = {
    "order": {"origin": "石家庄", "dest": "南京",
              "cargo": {"type": "普货", "weight_t": 8.0}},
    "source_text": "石家庄到南京 普货 8吨 优先走高速 尽量便宜",
}


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


def use_smart() -> bool:
    from sca.agents import SupplyChainGraph

    g = SupplyChainGraph()
    out = asyncio.run(g.plan_prepare(dict(STATE)))
    return bool((out.get("plan_context") or {}).get("use_smart"))


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    security._redis.flushall()
    old_val = None

    print("\n[1] 开关的基本语义")
    runtime_flags.set_flags(route_model=None, intent_model=None)
    check("未设置时默认允许（保持既有行为）", runtime_flags.get("route_model") is True)
    runtime_flags.set_flags(route_model=False)
    check("可关闭", runtime_flags.get("route_model") is False)
    check("未知键不影响", runtime_flags.get("nope", True) is True)
    runtime_flags.set_flags(route_model=None)

    print("\n[2] 内核：开着时会走模型增强选路")
    runtime_flags.set_flags(route_model=None)
    check("默认 use_smart=True（原话含路线偏好）", use_smart() is True)

    print("\n[3] 内核：关掉后不再走模型")
    runtime_flags.set_flags(route_model=False)
    check("关闭后 use_smart=False", use_smart() is False)

    print("\n[4] 管理接口改配置 -> 内核立即跟随")
    runtime_flags.set_flags(route_model=None)
    with TestClient(app) as c:
        tk = login(c, "admin")
        H = {"Authorization": f"Bearer {tk}"}
        r = c.get("/api/admin/config", headers=H)
        cur = next((i for i in (r.json() or {}).get("items", [])
                    if i["key"] == "model.enabled"), None)
        old_val = cur["value"] if cur else None

        r = c.put("/api/admin/config/model.enabled",
                  json={"value": {"intent": True, "route": False}}, headers=H)
        check("保存成功", r.status_code == 200, r.text[:140])
        check("进程开关已变 False", runtime_flags.get("route_model") is False,
              str(runtime_flags.snapshot()))
        check("内核随之关闭", use_smart() is False)

        r = c.put("/api/admin/config/model.enabled",
                  json={"value": {"intent": True, "route": True}}, headers=H)
        check("改回 True 后内核恢复", use_smart() is True)

        db = session_as("app")
        try:
            check("配置变更进审计日志",
                  db.query(SysAuditLog).filter(
                      SysAuditLog.action == "config_changed").count() >= 2)
            # 还原初始配置
            row = db.get(SystemConfig, "model.enabled")
            if row and old_val is not None:
                row.cfg_value = old_val
                db.commit()
            db.query(SysAuditLog).filter(
                SysAuditLog.action == "config_changed").delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    runtime_flags.set_flags(route_model=None, intent_model=None)
    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 条问题")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
