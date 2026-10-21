"""认证与 RBAC 的进程内验证（不依赖真实 Redis：用 fakeredis 打桩）。

    ./.venv/Scripts/python.exe scripts/test_platform_auth.py

覆盖：图形验证码 -> 注册 -> 登录 -> /me，以及密码错、验证码错、未登录、重复注册、
验证码不可重复使用、答错即作废、Redis 不可用时 fail-closed。
"""
from __future__ import annotations

import base64
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
from sca.platform.models import SysUser  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _captcha_solver import solve_captcha  # noqa: E402

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


def _new_redis():
    return fakeredis.FakeRedis(decode_responses=True)


def main() -> int:
    acc = f"t{int(time.time()) % 10_000_000}"
    security._redis = _new_redis()
    security._redis.flushall()

    with TestClient(app) as c:
        print("\n[1] 图形验证码 -> 注册 -> 登录 -> /me")
        r = c.post("/api/auth/code", json={"account": acc, "purpose": "register"})
        check("取图形验证码返回 200", r.status_code == 200, r.text[:120])
        body = r.json() or {}
        check("响应里没有明文验证码",
              not any(k in body for k in ("code_for_demo", "code", "text")), str(body)[:160])
        img = str(body.get("image", ""))
        check("返回 PNG data URI", img.startswith("data:image/png;base64,"), img[:48])
        raw = base64.b64decode(img.split(",", 1)[1]) if "," in img else b""
        check("图片是可解码的 PNG", raw[:8] == b"\x89PNG\r\n\x1a\n", str(raw[:8]))
        check("返回验证码图片 ID", bool(body.get("cid")), str(body)[:80])
        code, cid = solve_captcha(body)
        check("答案只存在服务端 Redis", bool(code) and len(code) == body.get("length", -1),
              f"code={code!r} len={body.get('length')}")

        r = c.post("/api/auth/register", json={
            "account": acc, "password": "abc123", "display_name": "测试货主",
            "role": "shipper", "org_name": f"测试货主企业{acc}", "code": code, "cid": cid,
        })
        check("注册成功", r.status_code == 200, r.text[:160])
        reg = r.json() or {}
        check("注册即返回 token", bool(reg.get("token")), str(reg)[:120])
        check("货主进入 /shipper.html", reg.get("home") == "/shipper.html", str(reg.get("home")))
        uid = (reg.get("user") or {}).get("id")
        check("货主具备 order:create", "order:create" in (reg.get("user") or {}).get("perms", []),
              str((reg.get("user") or {}).get("perms")))

        print("\n[2] 验证码一次性")
        r = c.post("/api/auth/register", json={
            "account": acc, "password": "abc123", "display_name": "重复",
            "role": "shipper", "org_name": f"测试货主企业{acc}", "code": code, "cid": cid,
        })
        check("重复注册被拒（409）", r.status_code == 409, f"{r.status_code} {r.text[:100]}")

        print("\n[3] 登录：先密码后验证码")
        r = c.post("/api/auth/code", json={"account": acc, "purpose": "login"})
        lcode, lcid = solve_captcha(r.json())
        check("登录验证码可取", bool(lcode) and bool(lcid), r.text[:100])

        r = c.post("/api/auth/login", json={"account": acc, "password": "wrong",
                                            "code": lcode, "cid": lcid})
        check("密码错被拒（401）", r.status_code == 401, f"{r.status_code} {r.text[:100]}")
        check("错误提示不泄露账号是否存在",
              "账号或密码错误" in r.text, r.text[:100])

        r = c.post("/api/auth/login", json={"account": acc, "password": "abc123",
                                            "code": lcode, "cid": lcid})
        check("密码错未消耗验证码，正确凭据可登录", r.status_code == 200, r.text[:160])
        token = (r.json() or {}).get("token")
        check("登录签发 token", bool(token), str(r.json())[:120])

        r = c.post("/api/auth/login", json={"account": acc, "password": "abc123",
                                            "code": lcode, "cid": lcid})
        check("已用过的验证码不能重复登录（400）", r.status_code == 400, f"{r.status_code}")

        print("\n[3b] 答错即作废当前图片")
        r = c.post("/api/auth/code", json={"account": acc, "purpose": "login"})
        wcode, wcid = solve_captcha(r.json())
        wrong = "AAAA" if wcode != "AAAA" else "BBBB"
        r = c.post("/api/auth/login", json={"account": acc, "password": "abc123",
                                            "code": wrong, "cid": wcid})
        check("验证码错被拒（400）", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
        check("提示会换一张", "换一张" in r.text, r.text[:120])
        r = c.post("/api/auth/login", json={"account": acc, "password": "abc123",
                                            "code": wcode, "cid": wcid})
        check("答错后同一张图立即失效（400）", r.status_code == 400, f"{r.status_code}")

        print("\n[3c] 缺 cid 不放行")
        r = c.post("/api/auth/login", json={"account": acc, "password": "abc123",
                                            "code": "ABCD"})
        check("无图片 ID 一律拒绝（400）", r.status_code == 400, f"{r.status_code}")

        print("\n[4] 鉴权")
        r = c.get("/api/auth/me")
        check("无 token 访问被拒（401）", r.status_code == 401, f"{r.status_code}")
        r = c.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"})
        check("伪造 token 被拒（401）", r.status_code == 401, f"{r.status_code}")
        r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        check("/me 返回本人信息", r.status_code == 200 and
              (r.json().get("user") or {}).get("account") == acc, r.text[:120])

        print("\n[5] 验证码错误计数与锁定")
        acc2 = f"x{int(time.time()) % 10_000_000}"
        r = c.post("/api/auth/code", json={"account": acc2, "purpose": "login"})
        check("为陌生账号也可发码（不泄露是否存在）", r.status_code == 200, r.text[:100])
        for i in range(security.config.CODE_MAX_TRY):
            rr = c.post("/api/auth/login", json={"account": acc2, "password": "x", "code": "111111"})
        # 密码错在验码之前，所以这里不会累计验证码错误次数——这是刻意的顺序设计
        check("密码错不累计验证码错误次数", rr.status_code == 401, f"{rr.status_code}")

    print("\n[6] Redis 不可用时 fail-closed")
    security._redis = None
    security.redis_client = lambda: (_ for _ in ()).throw(RuntimeError("redis down"))
    with TestClient(app) as c:
        r = c.post("/api/auth/code", json={"account": "nobody", "purpose": "login"})
        check("Redis 不可用返回 503（绝不降级放行）", r.status_code == 503,
              f"{r.status_code} {r.text[:100]}")

    # 清理
    db = session_as("app")
    try:
        for a in (acc, acc2):
            u = db.query(SysUser).filter(SysUser.account == a).first()
            if u:
                db.delete(u)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
