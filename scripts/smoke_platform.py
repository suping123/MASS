"""三平台一键自检（打真实服务，走真实 MySQL / Redis / PG）。

    ./.venv/Scripts/python.exe scripts/smoke_platform.py          # 默认 8000
    ./.venv/Scripts/python.exe scripts/smoke_platform.py 8001
    ./.venv/Scripts/python.exe scripts/smoke_platform.py --keep    # 保留测试数据

前置：服务已启动（start_all.bat），MySQL / Redis / PG 均在运行。
会创建一条测试订单并在结束时清理；--keep 可保留以便在浏览器里查看。

与 test_platform_flow.py 的区别：那个是进程内 + fakeredis 打桩的单元/集成测试；
这个是**真实 HTTP + 真实 Redis 验证码 + 真实 PG 向量**的端到端冒烟。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
PASSED = 0
PORT = 8000
# 只用于白盒读取图形验证码答案，业务请求一律走 HTTP
REDIS_URL = os.environ.get("SCA_REDIS_URL", "redis://127.0.0.1:6379/1")


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")
    return cond


def call(path: str, body=None, token: str = "", timeout: int = 40):
    url = f"http://127.0.0.1:{PORT}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None
    except Exception as e:  # noqa: BLE001 连不上要给出可读原因
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _agent_heartbeats() -> dict[str, bool]:
    """四个 agent 有没有活着的进程。白盒读 `agent_actor.last_seen_at`。

    为什么不调 ``/api/dispatch/agents/status``：那个接口 S6 起是给调度台看的
    （显示"谁没在跑"）。自检脚本要能在**服务没起来或 500** 的情况下也判断，
    所以直连库读心跳——和读验证码答案是同一类白盒操作，且更早发现故障。
    """
    try:
        from sca.platform.agents import bus
        from sca.platform.db import session_as
        from sca.platform.models import CarrierProfile
        from sqlalchemy import select
    except Exception:                       # noqa: BLE001 依赖缺失按"没起来"算
        return {"import": False}

    out: dict[str, bool] = {}
    db = session_as("app")
    try:
        out["dispatch"] = bus.alive(db, "dispatch")
        out["shipper"] = bus.alive(db, "shipper")
        out["exception"] = bus.alive(db, "exception")
        orgs = [int(x) for x in db.execute(select(CarrierProfile.org_id)).scalars().all()
                if x]
        out["carrier"] = any(bus.alive(db, f"carrier:{o}") for o in orgs)
    except Exception as e:                  # noqa: BLE001
        out["error"] = False
        print(f"        心跳探测失败：{type(e).__name__}: {e}")
    finally:
        db.close()
    return out


def solve_captcha(resp) -> tuple[str, str]:
    """从真 Redis 读出这张图的答案。生产路径不会回显答案，冒烟测试是白盒。"""
    import redis as _redis

    cid = str((resp or {}).get("cid") or "")
    if not cid:
        return "", ""
    r = _redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
    return str(r.get("sca:cap:" + cid) or ""), cid


def login(account: str) -> str:
    st, c = call("/api/auth/code", {"account": account, "purpose": "login"})
    if st != 200:
        raise RuntimeError(f"{account} 取验证码失败：{st} {c}")
    code, cid = solve_captcha(c)
    if not code:
        raise RuntimeError(f"{account} 验证码答案不在 Redis（cid={cid}）")
    st, lr = call("/api/auth/login", {"account": account, "password": "123456",
                                      "code": code, "cid": cid})
    if st != 200:
        raise RuntimeError(f"{account} 登录失败：{st} {lr}")
    return lr["token"]


def main(argv: list[str]) -> int:
    global PORT
    from datetime import datetime

    started = datetime.now().replace(microsecond=0)
    keep = "--keep" in argv
    ports = [a for a in argv if a.isdigit()]
    if ports:
        PORT = int(ports[0])

    print("=" * 68)
    print(f"三平台自检 · http://127.0.0.1:{PORT}")
    print("=" * 68)

    print("\n[0] 服务探活")
    st, h = call("/api/health", timeout=8)
    if not check("服务在运行", st == 200 and (h or {}).get("ok") is True,
                 f"{st} {json.dumps(h, ensure_ascii=False)[:160] if h else '无响应'}；"
                 "请先双击 start_all.bat"):
        print("\n服务未启动，后续检查无法进行。")
        return 1
    mysql_ok = (h.get("mysql") or {}).get("ok") is True
    redis_ok = (h.get("redis") or {}).get("ok") is True
    check("MySQL 连通", mysql_ok, str(h.get("mysql")))
    check("Redis 连通", redis_ok, str(h.get("redis")))

    # S5 起下单/挂牌/投标/接单都由常驻 agent 执行：它们没起来，自检会在第 3 步
    # 拿到 504。先探一次心跳，把"没起进程"和"业务坏了"分开——否则一堆 504
    # 看起来像系统故障，实际只是少双击一个 .bat。
    agents = _agent_heartbeats()
    check("四个 agent 进程在线（心跳 ≤90s）", all(agents.values()),
          str(agents) + "；请先双击 start_agents.bat")
    if not all(agents.values()):
        print("\nagent 未全部启动，下单/挂牌/接单会返回 504，后续检查无意义。")
        return 1

    print("\n[1] 三端登录（真实 Redis 验证码）")
    toks: dict[str, str] = {}
    for acc, role in (("shipper1", "shipper"), ("dispatcher1", "dispatcher"),
                      ("carrier1", "carrier")):
        try:
            toks[role] = login(acc)
            check(f"{acc} 登录并签发令牌", bool(toks[role]))
        except RuntimeError as e:
            msg = str(e)
            if "429" in msg:
                msg += "（60 秒内重复发码会限流，等一分钟再跑）"
            check(f"{acc} 登录", False, msg)
    if len(toks) < 3:
        return 1

    print("\n[2] 货主端：对话下单（不足则反问，齐全则复述，确认才落单）")
    st, d = call("/api/shipper/chat", {"text": "我要发一批货"}, toks["shipper"])
    check("信息不足时反问", st == 200 and d.get("state") == "collecting",
          f"{st} {str(d)[:120]}")
    sid = (d or {}).get("session_id")

    st, d = call("/api/shipper/chat", {
        "session_id": sid,
        "text": "石家庄到南京，冷链 8 吨，明天下午6点前送到，预算1.2万"}, toks["shipper"])
    check("补齐后进入确认态并复述", st == 200 and d.get("state") == "confirming",
          f"{st} {str(d)[:160]}")
    check("复述含六项信息", len(((d or {}).get("data") or {}).get("summary") or []) == 6,
          str((d or {}).get("data"))[:160])

    st, d = call("/api/shipper/chat", {"session_id": sid, "text": "确认"}, toks["shipper"])
    oid = (d or {}).get("order_id")
    check("确认后落单", st == 200 and bool(oid), f"{st} {str(d)[:160]}")

    print("\n[3] 调度端：出方案 → 定价挂牌")
    st, p = call(f"/api/dispatch/orders/{oid}/plan", {}, toks["dispatcher"])
    plan = (p or {}).get("plan") or {}
    check("生成方案（含里程与 ETA）", st == 200 and plan.get("distance_km"),
          f"{st} {str(p)[:160]}")
    st, pb = call(f"/api/dispatch/orders/{oid}/publish", {}, toks["dispatcher"])
    check("定价挂牌", st == 200 and pb.get("publish_price", 0) > 0,
          f"{st} {str(pb)[:160]}")
    price = float((pb or {}).get("publish_price") or 0)
    if st == 200:
        gross, sfee, price = pb["gross"], pb["shipper_fee"], pb["publish_price"]
        check("挂牌价 = 预算 − 2% 服务费", abs(price - (gross - sfee)) < 0.01,
              f"{gross} - {sfee} != {price}")

    print("\n[3b] 竞价看板与 Agent 状态（S6 页面的数据源）")
    st, board = call("/api/dispatch/tenders?status=active", token=toks["dispatcher"])
    row = next((i for i in ((board or {}).get("items") or []) if i["order_id"] == oid), None)
    check("看板能看到该单在收标",
          st == 200 and row is not None and row.get("status") == "open",
          f"{st} {str(board)[:200]}")
    check("看板可付上限 = 挂牌价（不是货主预算）",
          row is not None and abs(float(row.get("budget") or 0) - price) < 0.01,
          f"{(row or {}).get('budget')} vs {price}")
    st, ags = call("/api/dispatch/agents/status", token=toks["dispatcher"])
    kinds = {a.get("kind") for a in ((ags or {}).get("agents") or [])}
    check("agent 状态接口覆盖四类且都在线",
          st == 200 and {"dispatch", "exception", "shipper", "carrier"} <= kinds
          and (ags or {}).get("any_offline") is False,
          f"{st} {sorted(kinds)} {str((ags or {}).get('offline'))[:120]}")
    st, _ = call("/api/dispatch/tenders", token=toks["carrier"])
    check("承运商不能看调度竞价看板（403）", st == 403, str(st))
    st, mb = call("/api/carrier/my-bids", token=toks["carrier"])
    check("承运商可读我的竞价", st == 200 and isinstance(mb, list),
          f"{st} {str(mb)[:120]}")

    print("\n[4] 承运商端：池子 → 试算 → 接单")
    st, pool = call("/api/carrier/pool", token=toks["carrier"])
    mine = next((i for i in (pool or []) if i["order_id"] == oid), None)
    check("池子里能看到该单", st == 200 and mine is not None,
          f"{st} {str(pool)[:160]}")
    check("池子里**不含**货主预算", all("budget_max" not in i for i in (pool or [])),
          str((pool or [])[:1])[:160])

    st, q = call(f"/api/carrier/orders/{oid}/quote", {}, toks["carrier"])
    check("收益试算", st == 200 and "profit" in (q or {}), f"{st} {str(q)[:160]}")
    if st == 200:
        print(f"        运费 ¥{q['quoted_price']:,} − 服务费 ¥{q['carrier_fee']:,} "
              f"− 成本 ¥{q['est_cost']:,} = 收益 ¥{q['profit']:,}")

    st, ac = call(f"/api/carrier/orders/{oid}/accept",
                  {"position_text": "石家庄正定物流园"}, toks["carrier"])
    check("接单（自报位置）", st == 200 and ac.get("status") == "claimed",
          f"{st} {str(ac)[:160]}")
    st, again = call(f"/api/carrier/orders/{oid}/accept",
                     {"position_text": "重复接单"}, toks["carrier"])
    check("重复接单被拒（幂等）", st == 409, f"{st}")

    # S5：成交类写操作必须留痕。接单/挂牌在 agent_message 里各有一条 invoke
    # （人 -> agent）与一条回复（agent -> 人）。没有这两条，说明又被直连了。
    st, th = call(f"/api/dispatch/agents/thread?order_id={oid}", token=toks["dispatcher"])
    msgs = (th or {}).get("messages") or []
    types = [(m.get("from"), m.get("to"), m.get("type")) for m in msgs]
    check("成交走消息总线：有 invoke 委托记录",
          any(t[2] == "invoke" for t in types), str(types)[:220])
    check("接单由承运商 agent 执行并回执",
          any(str(t[0]).startswith("carrier:") and str(t[1]).startswith("human:")
              for t in types), str(types)[:220])
    check("挂牌由调度 agent 执行",
          any(t[0] == "dispatch" and t[1].startswith("human:") for t in types),
          str(types)[:220])

    print("\n[4b] 三端订单详情（详情抽屉的数据源）")
    st, sd = call(f"/api/shipper/orders/{oid}", token=toks["shipper"])
    check("货主详情：含费用三段与流转记录",
          st == 200 and bool((sd or {}).get("publish")) and bool((sd or {}).get("events")),
          f"{st} {str(sd)[:200]}")
    check("货主详情：看不到承运商成本",
          not any(k in (sd or {}) for k in ("linehaul_cost", "deadhead_cost", "est_profit")),
          str(sorted((sd or {}).keys()))[:220])
    check("货主详情：含路线途经点",
          len(((sd or {}).get("plan") or {}).get("waypoints") or []) >= 2,
          str((sd or {}).get("plan"))[:160])

    st, cd = call(f"/api/carrier/orders/{oid}", token=toks["carrier"])
    check("承运商详情：含收益四段",
          st == 200 and bool((cd or {}).get("bid"))
          and ((cd or {}).get("bid") or {}).get("deadhead_cost") is not None,
          f"{st} {str(cd)[:200]}")
    check("承运商详情：看不到货主预算", "budget_max" not in (cd or {}),
          str(sorted((cd or {}).keys()))[:220])

    st, dd = call(f"/api/dispatch/orders/{oid}", token=toks["dispatcher"])
    check("调度详情：含途经点/投标/流转记录",
          st == 200 and len(((dd or {}).get("plan") or {}).get("waypoints") or []) >= 2
          and bool((dd or {}).get("bids")) and bool((dd or {}).get("events")),
          f"{st} {str(dd)[:200]}")

    st, pf = call("/api/carrier/profile", token=toks["carrier"])
    check("承运档案可读（单价参与收益试算）",
          st == 200 and (pf or {}).get("cost_per_km"), f"{st} {str(pf)[:160]}")

    st, kh = call("/api/dispatch/knowledge/history?limit=5", token=toks["dispatcher"])
    check("知识库构建记录可读", st == 200 and isinstance(kh, list), f"{st} {str(kh)[:160]}")

    print("\n[5] 承运商报异常（对话）→ 调度检索 SOP → 处置")
    st, d = call("/api/carrier/chat",
                 {"text": "我在保定服务区，冷藏机组故障，温度上来了"}, toks["carrier"])
    sid2 = (d or {}).get("session_id")
    if (d or {}).get("state") == "collecting":
        st, d = call("/api/carrier/chat",
                     {"session_id": sid2, "text": f"订单是 {mine['order_no']}"},
                     toks["carrier"])
    st, d = call("/api/carrier/chat", {"session_id": d.get("session_id"), "text": "确认"},
                 toks["carrier"])
    check("异常上报完成", st == 200 and (d or {}).get("done") is True,
          f"{st} {str(d)[:160]}")

    st, exc = call("/api/dispatch/exceptions?status=open", token=toks["dispatcher"])
    my_exc = next((e for e in (exc or []) if e["order_id"] == oid), None)
    check("调度端能看到该异常", st == 200 and my_exc is not None,
          f"{st} {str(exc)[:160]}")

    if my_exc:
        st, sop = call(f"/api/dispatch/exceptions/{my_exc['id']}/sop",
                       token=toks["dispatcher"])
        check("能检索到处置 SOP", st == 200 and len((sop or {}).get("hits") or []) > 0,
              f"{st} {str(sop)[:200]}")
        if st == 200 and sop.get("hits"):
            h0 = sop["hits"][0]
            print(f"        建议 {h0['sop_id']} {h0['title']} "
                  f"（渠道 {sop.get('channel')}，match={h0.get('match')}）")

        st, rv = call(f"/api/dispatch/exceptions/{my_exc['id']}/resolve",
                      {"resolution": "就近维修后继续运输，预计延误 2 小时",
                       "action": "resume"}, toks["dispatcher"])
        check("处置成功且订单回到在途", st == 200 and rv.get("order_status") == "in_transit",
              f"{st} {str(rv)[:160]}")

    print("\n[5b] 异常处置三动作：改道预览 / 延长时效 / 改派条件")
    # 再报一条异常：改道与改期都会真的改数据（方案版本、承诺时间），
    # 不能靠"只写一个 action 字段"糊过去，所以必须走一遍真实链路。
    st, d2 = call("/api/carrier/chat",
                  {"text": f"订单 {mine['order_no']} 在徐州服务区遇到暴雨，路面积水"},
                  toks["carrier"])
    if (d2 or {}).get("state") == "collecting":
        st, d2 = call("/api/carrier/chat",
                      {"session_id": (d2 or {}).get("session_id"),
                       "text": f"订单是 {mine['order_no']}"}, toks["carrier"])
    if (d2 or {}).get("state") != "done":
        st, d2 = call("/api/carrier/chat",
                      {"session_id": (d2 or {}).get("session_id"), "text": "确认"},
                      toks["carrier"])
    check("第二条异常上报完成", st == 200 and (d2 or {}).get("done") is True,
          f"{st} {str(d2)[:160]}")

    st, exc2 = call("/api/dispatch/exceptions?status=open", token=toks["dispatcher"])
    e2 = next((e for e in (exc2 or []) if e["order_id"] == oid), None)
    check("调度端看到第二条异常", st == 200 and e2 is not None,
          f"{st} {str(exc2)[:160]}")

    if e2:
        st, pv = call(f"/api/dispatch/exceptions/{e2['id']}/reroute-preview", {},
                      toks["dispatcher"])
        ok_shape = (pv or {}).get("ok") is True and {"old", "new", "detour_km"} <= set(pv or {})
        bad_shape = (pv or {}).get("ok") is False and bool((pv or {}).get("reason"))
        check("改道预览结构可渲染（成功带对比 / 失败带原因）",
              st == 200 and (ok_shape or bad_shape), f"{st} {str(pv)[:200]}")
        if ok_shape:
            print(f"        绕开 {pv['avoid_city']}：{pv['old']['distance_km']} → "
                  f"{pv['new']['distance_km']} km（{pv['detour_km']:+} km）")
        else:
            print(f"        未生成方案（如实拒绝）：{(pv or {}).get('reason')}")

        st, op = call(f"/api/dispatch/reassign/options?order_id={oid}",
                      token=toks["dispatcher"])
        trg = (op or {}).get("triggers") or []
        check("改派选项 5 个触发码且都带判定理由",
              st == 200 and len(trg) == 5 and all(t.get("why") for t in trg),
              f"{st} {str(trg)[:200]}")

        st, rv2 = call(f"/api/dispatch/exceptions/{e2['id']}/resolve",
                       {"resolution": "客户同意晚半天收货，承诺时效顺延 4 小时",
                        "action": "extend", "delay_h": 4}, toks["dispatcher"])
        dl = (rv2 or {}).get("deadline") or {}
        check("改期成功且订单仍在途",
              st == 200 and (rv2 or {}).get("order_status") == "in_transit",
              f"{st} {str(rv2)[:200]}")
        if dl.get("before") and dl.get("after"):
            from datetime import datetime as _dt

            hours = ((_dt.fromisoformat(dl["after"]) - _dt.fromisoformat(dl["before"])
                      ).total_seconds() / 3600)
            check("承诺时间正好延后 4 小时", abs(hours - 4) < 0.01, f"实际 {hours}")
            print(f"        承诺 {dl['before']} → {dl['after']}")
        else:
            check("改期返回前后承诺时间", False, str(dl)[:200])

        st, nb = call("/api/notifications?limit=10", token=toks["shipper"])
        types = [i.get("ntype") for i in (nb or {}).get("items") or []]
        check("货主收到改期通知", st == 200 and "deadline_changed" in types, str(types))

    print("\n[6] 知识库（PG + pgvector）")
    st, ks = call("/api/dispatch/knowledge/stats", token=toks["dispatcher"])
    check("知识库可用", st == 200 and (ks or {}).get("available") is True,
          f"{st} {str(ks)[:160]}")
    if st == 200:
        print(f"        块数 {ks.get('chunks')}，模型 {ks.get('model_in_db')}，"
              f"一致性 {ks.get('consistent')}")
    st, sr = call("/api/dispatch/knowledge/search",
                  {"query": "冷藏车温度超过上限怎么办", "top_k": 2}, toks["dispatcher"])
    check("检索返回结果", st == 200 and len((sr or {}).get("hits") or []) > 0,
          f"{st} {str(sr)[:160]}")
    if st == 200 and sr.get("hits"):
        print(f"        渠道 {sr.get('channel')}，top1 {sr['hits'][0]['sop_id']} "
              f"{sr['hits'][0]['title']}")

    print("\n[7] 权限隔离")
    st, _ = call("/api/dispatch/orders", token=toks["shipper"])
    check("货主不能看调度全量（403）", st == 403, str(st))
    st, _ = call("/api/carrier/pool", token=toks["dispatcher"])
    check("调度员没有承运商权限（403）", st == 403, str(st))
    st, _ = call("/api/shipper/orders", token=toks["carrier"])
    check("承运商不能看货主订单（403）", st == 403, str(st))

    print("\n[7b] 智能调度助手（对话式调度）")
    st, ov = call("/api/dispatch/overview", token=toks["dispatcher"])
    check("概览接口可用", st == 200 and isinstance((ov or {}).get("total"), int),
          f"{st} {str(ov)[:120]}")
    st, c1 = call("/api/dispatch/chat", {"text": "看一下整体情况"}, toks["dispatcher"])
    r1 = (c1 or {}).get("reply", "")
    check("概览对话有回复", st == 200 and "订单" in r1, f"{st} {r1[:120]}")
    check("回复带决策轨迹", bool((c1 or {}).get("trace")), str((c1 or {}).get("trace"))[:120])
    st, c2 = call("/api/dispatch/chat", {"text": "冷链和生鲜有什么区别"}, toks["dispatcher"])
    r2 = (c2 or {}).get("reply", "")
    check("能解释货类区别", st == 200 and "区别" in r2 and "生鲜" in r2, r2[:120])
    st, c3 = call("/api/dispatch/chat", {"text": "温度超标了怎么处理"}, toks["dispatcher"])
    r3 = (c3 or {}).get("reply", "")
    check("异常处置能给出分级或 SOP",
          st == 200 and ("SOP" in r3 or "等级" in r3 or "P0" in r3 or "P1" in r3 or "P2" in r3),
          r3[:160])
    if oid:
        st, ol = call("/api/dispatch/orders", token=toks["dispatcher"])
        row = next((x for x in (ol or []) if x.get("id") == oid), None)
        if row:
            st, c4 = call("/api/dispatch/chat", {"text": f"{row['order_no']} 这单怎么样"},
                          toks["dispatcher"])
            r4 = (c4 or {}).get("reply", "")
            check("能解读指定订单", st == 200 and row["order_no"] in r4, r4[:160])
            check("解读带上下文卡片",
                  ((c4 or {}).get("data") or {}).get("focus") is not None, str((c4 or {}).get("data"))[:120])

    # ---- 清理 ----
    if not keep and oid:
        print("\n[8] 清理测试数据")
        from sca.platform.db import session_as
        from sca.platform.models import (AgentMessage, CarrierBid, ChatMessage,
                                         ChatSession, ExceptionReport, Notification,
                                         OrderEvent, OrderPlan, OrderPublish,
                                         Settlement, ShipperOrder, Tender)
        db = session_as("app")
        try:
            # 顺序按外键依赖：publish 引用 plan，plan 必须在 publish 之后删
            counts = {}
            for model in (Tender, CarrierBid, OrderEvent, ExceptionReport, Notification,
                          Settlement, ChatSession, OrderPublish, OrderPlan):
                counts[model.__name__] = db.query(model).filter(
                    model.order_id == oid).delete()
            # 这笔业务的 Agent 消息（不带 order_id 列，按 thread_id 认）
            counts["AgentMessage"] = db.query(AgentMessage).filter(
                AgentMessage.thread_id == f"order:{oid}").delete()
            counts["ShipperOrder"] = db.query(ShipperOrder).filter(
                ShipperOrder.id == oid).delete()
            # 只删本次自检期间新建的会话；用户自己的对话不能被清掉
            uids = [r.id for r in db.query(ChatSession).filter(
                ChatSession.created_at >= started).all()]
            if uids:
                db.query(ChatMessage).filter(ChatMessage.session_id.in_(uids)).delete(
                    synchronize_session=False)
                db.query(ChatSession).filter(ChatSession.id.in_(uids)).delete(
                    synchronize_session=False)
            db.commit()
            # 逐表报数：只说"已清理"而没删干净，下次跑就会多出一张假单挂着
            left = db.query(ShipperOrder).filter(ShipperOrder.id == oid).count()
            print("        " + " / ".join(f"{k} {v}" for k, v in counts.items() if v))
            check("测试订单确实删掉了（不是只打印一句已清理）", left == 0, f"残留 {left} 张")
        except Exception as e:  # noqa: BLE001 清理失败不影响结论，但要说明
            db.rollback()
            print(f"        清理失败（不影响结论）：{type(e).__name__}: {e}")
        finally:
            db.close()
    elif keep:
        print(f"\n[8] 保留测试数据：订单 id={oid}，可在浏览器里查看")

    print("\n" + "=" * 68)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项不通过")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{PASSED} 项检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
