"""演示：把承运商进程杀掉，接单就做不成了；把它拉起来，又能做了。

    ./.venv/Scripts/python.exe scripts/demo_kill_agent.py
    ./.venv/Scripts/python.exe scripts/demo_kill_agent.py --keep

前置：服务已起（start_platform.bat）。**四个 agent 由本脚本自己拉起并按需杀掉**，
所以跑之前先 ``start_agents.bat --stop``，否则会跟已有的进程抢同一个收件箱。

它要说明的事（第 5 条检验的进程级证据）
------------------------------------
S4 那份演示证明的是"杀掉 dispatch，carrier 照常干活"（故障域隔离）。
这一份证明的是**反向**的依赖：成交必须经过 agent，进程不在就真的做不成。
两件事缺一不可——只隔离不依赖，那四个进程就是可选的装饰。

看点：
  [3] 杀掉 carrier → `POST /accept` 返回 504，订单仍是 published，库里没多出 bid
  [4] 起来之后同一个人再点接单 → 200 claimed
  [5] 回放这笔业务的 agent_message：人类的 invoke、agent 的 accept 都在
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 8000
KINDS = ("dispatch", "carrier", "exception", "shipper")


def base() -> str:
    return f"http://127.0.0.1:{PORT}"


def call(path: str, payload=None, token: str = "", method: str = "",
         timeout: int = 40):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base() + path, data=data,
                                method=method or ("POST" if data is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:                       # noqa: BLE001
            return e.code, None
    except Exception as e:                      # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def redis_answer(cid: str) -> str:
    """白盒读验证码答案（与服务端同一个 Redis）。真人看图，脚本读库。"""
    import redis as _redis

    url = os.environ.get("SCA_REDIS_URL", "redis://127.0.0.1:6379/1")
    r = _redis.Redis.from_url(url, decode_responses=True, socket_timeout=3)
    return str(r.get("sca:cap:" + cid) or "")


def login(account: str) -> str:
    st, body = call("/api/auth/captcha?purpose=login&account=" + account, method="GET")
    if st != 200:
        raise SystemExit(f"取验证码失败：{st} {body}")
    code = redis_answer(str((body or {}).get("cid") or ""))
    st, r = call("/api/auth/login", {"account": account, "password": "123456",
                                     "code": code, "cid": (body or {}).get("cid")})
    if st != 200:
        raise SystemExit(f"{account} 登录失败：{st} {r}")
    return r["token"]


# ---------------------------------------------------------------------------
# 进程管理：自己起、自己杀，不依赖 launcher（演示要能确定"杀的是哪一个"）
# ---------------------------------------------------------------------------
def spawn(kind: str) -> subprocess.Popen:
    log = open(ROOT / "logs" / f"demo_{kind}.log", "ab", buffering=0)
    p = subprocess.Popen(
        [sys.executable, "-m", "sca.platform.agents.worker", "--kind", kind,
         "--no-echo"], cwd=str(ROOT), stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    print(f"        {kind:<9} pid={p.pid}")
    return p


def main(argv: list[str]) -> int:
    global PORT
    keep = "--keep" in argv
    ports = [a for a in argv if a.isdigit()]
    if ports:
        PORT = int(ports[0])

    print("=" * 68)
    print(f"进程级移除测试 · http://127.0.0.1:{PORT}")
    print("=" * 68)

    for d in ("logs",):
        (ROOT / d).mkdir(exist_ok=True)

    procs: dict[str, subprocess.Popen] = {}
    try:
        print("\n[1] 拉起四个 agent（本脚本自己管生命周期）")
        for k in KINDS:
            procs[k] = spawn(k)
        time.sleep(4.0)

        print("\n[2] 准备一笔已挂牌的单（下单/挂牌都经 agent 执行）")
        t_ship, t_disp, t_car = login("shipper1"), login("dispatcher1"), login("carrier1")
        st, d = call("/api/shipper/chat",
                     {"text": "石家庄到南京 普货 10吨 40小时内送到 预算5000元"}, t_ship)
        st, d = call("/api/shipper/chat", {"session_id": (d or {}).get("session_id"),
                                           "text": "确认"}, t_ship)
        oid = int((d or {}).get("order_id") or 0)
        print(f"        订单 id={oid} 号={d.get('order_id') and 'ok'}")
        call(f"/api/dispatch/orders/{oid}/plan", {}, t_disp)
        st, pub = call(f"/api/dispatch/orders/{oid}/publish", {}, t_disp)
        print(f"        挂牌 {st}：挂牌价 ¥{(pub or {}).get('publish_price')}")
        if st != 200:
            print("        挂牌没成功，后面的演示没有意义")
            return 1

        print("\n[3] 杀掉承运商进程，再点接单")
        procs["carrier"].terminate()
        try:
            procs["carrier"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            procs["carrier"].kill()
        print(f"        carrier pid={procs['carrier'].pid} 已退出")
        t0 = time.monotonic()
        st, body = call(f"/api/carrier/orders/{oid}/accept",
                        {"position_text": "石家庄正定物流园"}, t_car)
        cost = time.monotonic() - t0
        print(f"        接单 -> {st}（{cost:.2f}s）")
        print(f"        原因：{(body or {}).get('detail') or body}")
        st2, od = call(f"/api/dispatch/orders/{oid}", token=t_disp)
        status_now = (od or {}).get("status")
        print(f"        订单状态仍是：{status_now}")
        ok3 = st == 504 and status_now == "published"
        print(f"        {'PASS' if ok3 else 'FAIL'}  没有承运商 agent，接单做不成")

        print("\n[4] 把承运商拉起来，同一个人再点接单")
        # 上一步那条 invoke 还躺在收件箱里。真实运维里请求方早就超时了，
        # 这里把它标成"已超时"（与 SCA_AGENT_STALE_S 的处理口径一致），
        # 免得重启后它自己把单接了，看不出"人重新点一次"的效果。
        n = _expire_pending("carrier")
        print(f"        清掉超时的委托 {n} 条")
        procs["carrier"] = spawn("carrier")
        time.sleep(3.5)
        t0 = time.monotonic()
        st, body = call(f"/api/carrier/orders/{oid}/accept",
                        {"position_text": "石家庄正定物流园"}, t_car)
        cost = time.monotonic() - t0
        print(f"        接单 -> {st}（{cost:.2f}s）{(body or {}).get('status')}")
        ok4 = st == 200 and (body or {}).get("status") == "claimed"
        print(f"        {'PASS' if ok4 else 'FAIL'}  agent 回来后接单成功")

        print("\n[5] 回放这笔业务的消息（多 agent 协作的原始证据）")
        st, th = call(f"/api/dispatch/agents/thread?order_id={oid}", token=t_disp)
        for m in (th or {}).get("messages") or []:
            print(f"        #{m['id']:<5} {m['from']:<12} -> {m['to']:<12} "
                  f"{m['type']:<8} {m['type_label']}")
        types = [m["type"] for m in ((th or {}).get("messages") or [])]
        ok5 = "invoke" in types and "accept" in types
        print(f"        {'PASS' if ok5 else 'FAIL'}  人委托 + agent 回执都在库里")

        print("\n" + "=" * 68)
        print("结论：成交确实经过承运商 agent —— 杀掉它就做不成，拉起来就能做成。")
        return 0 if (ok3 and ok4 and ok5) else 1
    finally:
        print("\n[6] 收尾：停掉本脚本拉起的进程")
        for k, p in procs.items():
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("        已停止")


def _expire_pending(prefix: str) -> int:
    """把发给某类 agent 的未处理消息标成已失败（请求方早已超时）。"""
    from sqlalchemy import select

    from sca.platform.db import session_as
    from sca.platform.models import AgentMessage

    db = session_as("app")
    try:
        rows = db.execute(select(AgentMessage).where(
            AgentMessage.to_agent.like(prefix + "%"),
            AgentMessage.status.in_(("pending", "working")))).scalars().all()
        for m in rows:
            m.status = "failed"
            m.error = "请求方已超时（504），委托作废"
        db.commit()
        return len(rows)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
