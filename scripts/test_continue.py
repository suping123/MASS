"""「重新处理」端到端自检（真实 HTTP 打本机 API，不 mock）。

覆盖需求：处理旧会话时**会话 ID 不变**，且可反复重新处理——
  1) P1 全生命周期：触发 -> 审批 -> 调度审核驳回(rejected) -> **同一会话**原地重处理
     -> 再审批 -> 审核通过 -> done；全程 thread_id 不变、不产生新会话、通知不重复推送；
  2) blocked 会话：原样重处理被拒（无复用运单）-> 补录起讫地后**同一会话**解锁；
  3) 边界：修订文本仍无起讫地 / 手机号非法（会话状态不变）、缺参、会话不存在、
     awaiting_* 提示走 /resume、done 拒绝、并发防重入。

用法：
    python scripts/test_continue.py            # 默认打 http://127.0.0.1:8000
    python scripts/test_continue.py 8010       # 指定端口

注意：T1 走完完整生命周期会**真实推送 2 条钉钉调度通知**（司机 / 收货方），
与 scripts/test_sessions.py 的行为一致；不需要真发时请临时关闭
「流程开关 -> 调度员审核后下发」或把最后的审核改为驳回。
测试自建自删会话，不动历史里已有的演示数据。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = f"http://127.0.0.1:{PORT}"
API = BASE + "/api"
HTTP_TIMEOUT = 120.0         # 图执行 + HITL 多轮 + 模型化应急重规划（12-30s/次），放宽超时

PASS, FAIL = [], []
CREATED: list = []           # 本测试创建的会话 id，收尾统一删除


def check(name: str, cond: bool, extra: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"{'  [PASS]' if cond else '  [FAIL]'} {name}" + (f" — {extra}" if extra else ""))
    return cond


def req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "detail": exc.read().decode("utf-8", "ignore")[:200]}
    except Exception as exc:  # noqa: 连接失败等
        return {"_conn_error": str(exc)}


def sid_of(r: dict) -> str:
    return str(r.get("thread_id") or "")


def cur_total() -> int:
    return (req("GET", "/sessions?limit=1") or {}).get("total") or 0


def expect_total() -> int:
    """预期总数 = 初始总数 + 本测试已创建且未删除的会话数（重处理本身不得新增）。"""
    return (total0 or 0) + len(CREATED)


def brief_status(sid: str) -> str:
    d = req("GET", "/sessions/" + sid)
    return (d.get("session") or {}).get("status") or ""


def detail(sid: str) -> dict:
    return (req("GET", "/sessions/" + sid).get("session") or {})


P1_TEXT = "从广州发往成都的冷藏疫苗，运输途中制冷机组故障"   # 命中 E-CAP-BREAK / P1
BLOCK_TEXT = "帮我安排一批货物运输"                        # 无起讫地 -> 受理阻断
REVISED_TEXT = "帮我安排一批货物运输，从杭州发往南京的普货 5 吨，希望明天送达"


print("=" * 74)
print("重新处理（同会话原地重跑）自检 ·", API)
print("=" * 74)

# 0) 服务可用
h = req("GET", "/health")
if h.get("_http_error") or h.get("_conn_error"):
    print(f"[FATAL] /api/health 不可用：{h}. 请先启动服务（start_server.bat）")
    sys.exit(2)
print(f"[info] health: hitl={h.get('hitl_enabled')} model_available={h.get('model_available')}")

total0 = (req("GET", "/sessions?limit=1") or {}).get("total")
print(f"[info] 当前会话总数：{total0}")

# ---------------------------------------------------------------------------
print("\n■ T1 P1 全生命周期：驳回 -> 同一会话原地重处理 -> 审批 -> 审核 -> done")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid = sid_of(r)
CREATED.append(sid)
check("T1.1 触发 P1 并挂起审批", bool(sid) and r.get("interrupted") is True,
      f"thread_id={sid} interrupt={json.dumps(r.get('interrupt'), ensure_ascii=False)[:80]}")

c = req("POST", "/sessions/continue", {"session_id": sid})
check("T1.2 挂起态会话拒绝重处理（提示走 /resume）",
      bool(c.get("error")) and c.get("still_live") is True, c.get("error") or "")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": True, "by": "test"}})
check("T1.3 审批通过 -> 进入调度结果审核", r.get("interrupted") is True
      and (r.get("interrupt") or {}).get("type") == "dispatch_review",
      f"interrupt={json.dumps(r.get('interrupt'), ensure_ascii=False)[:80]}")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": False, "by": "test"}})
check("T1.4 调度员驳回 -> 会话 rejected", r.get("interrupted") is False,
      f"thread_id={r.get('thread_id')}")
d = detail(sid)
check("T1.5 落盘状态为 rejected 且无续接指针",
      d.get("status") == "rejected" and not d.get("continued_by")
      and not d.get("parent_session_id"), f"status={d.get('status')}")

# —— 关键：原地重处理（不带修订文本，复用原运单与异常）——
c = req("POST", "/sessions/continue", {"session_id": sid})
check("T1.6 重处理返回同一会话 ID（thread_id 不变）",
      sid_of(c) == sid and c.get("reprocessed") is True,
      f"thread_id={c.get('thread_id')} reprocessed={c.get('reprocessed')} "
      f"checkpoint_purged={c.get('checkpoint_purged')}")
check("T1.7 重处理后重新挂起等待审批（同线程）", c.get("interrupted") is True,
      f"interrupt={json.dumps(c.get('interrupt'), ensure_ascii=False)[:80]}")
d = detail(sid)
ev_types = [e.get("type") for e in (d.get("events") or [])]
check("T1.8 时间线含 reprocess 事件且状态回挂起",
      "reprocess" in ev_types and d.get("status") == "awaiting_approval",
      f"status={d.get('status')} events={ev_types}")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": True, "by": "test"}})
check("T1.9 再次审批通过 -> 调度结果审核", r.get("interrupted") is True
      and (r.get("interrupt") or {}).get("type") == "dispatch_review")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": True, "by": "test"}})
check("T1.10 审核通过 -> 处置完成", r.get("interrupted") is False, f"thread_id={r.get('thread_id')}")
d = detail(sid)
notis = d.get("notifications") or []
check("T1.11 终态 done 且通知恰好 2 条（旧检查点已清，无重复推送）",
      d.get("status") == "done" and len(notis) == 2 and all(n.get("sent") for n in notis),
      f"status={d.get('status')} notifications={len(notis)}")
check("T1.12 同一会话时间线：两次触发 + 一次重处理",
      ev_types.count("trigger") == 2 and ev_types.count("reprocess") == 1, f"events={ev_types}")
check("T1.13 全程未产生新会话（总数 = 初始 + 本测试自建）",
      cur_total() == expect_total(), f"total={cur_total()} expect={expect_total()}")

c = req("POST", "/sessions/continue", {"session_id": sid})
check("T1.14 done 会话拒绝重处理（避免重复下发）", bool(c.get("error")), c.get("error") or "")

# ---------------------------------------------------------------------------
print("\n■ T2 blocked 会话：原样重处理被拒 -> 补录起讫地后同会话解锁")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": BLOCK_TEXT})
sid2 = sid_of(r)
CREATED.append(sid2)
check("T2.1 无起讫地需求被阻断（blocked）",
      bool(sid2) and r.get("need_input") is True and brief_status(sid2) == "blocked",
      f"thread_id={sid2} status={brief_status(sid2)}")

c = req("POST", "/sessions/continue", {"session_id": sid2})
check("T2.2 无复用运单时拒绝重处理且状态不变",
      bool(c.get("error")) and c.get("session_unchanged") is True
      and brief_status(sid2) == "blocked", c.get("error") or "")
d2 = detail(sid2)
check("T2.3 拒绝重处理不写任何事件（时间线未被污染）",
      not any(e.get("type") == "reprocess" for e in (d2.get("events") or [])),
      f"events={[e.get('type') for e in (d2.get('events') or [])]}")

c = req("POST", "/sessions/continue", {"session_id": sid2, "text": REVISED_TEXT})
check("T2.4 补录起讫地后重处理：仍是同一会话 ID",
      sid_of(c) == sid2 and c.get("reprocessed") is True and c.get("input_revised") is True,
      f"thread_id={c.get('thread_id')}")
d2 = detail(sid2)
check("T2.5 补录后解除阻断（进入审核挂起或完成）",
      d2.get("status") in ("awaiting_review", "done", "awaiting_approval"),
      f"status={d2.get('status')}")
check("T2.6 需求原文已更新为修订版", (d2.get("input") or {}).get("text") == REVISED_TEXT,
      f"text={(d2.get('input') or {}).get('text')}")
# 终止这条会话（审核驳回，不推钉钉），保持现场干净
r = req("POST", "/resume", {"thread_id": sid2, "decision": {"approved": False, "by": "test"}}) \
    if d2.get("status") == "awaiting_review" else {}
check("T2.7 blocked 解锁全程未产生新会话",
      cur_total() == expect_total(), f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ T3 边界：修订文本仍无起讫地 / 手机号非法（会话状态不变）")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": BLOCK_TEXT})
sid3 = sid_of(r)
CREATED.append(sid3)
c = req("POST", "/sessions/continue", {"session_id": sid3, "text": "还是没说去哪儿"})
check("T3.1 修订文本仍解析不出起讫地 -> need_input 且会话不变",
      c.get("need_input") is True and c.get("session_unchanged") is True
      and brief_status(sid3) == "blocked", c.get("error") or "")
c = req("POST", "/sessions/continue",
        {"session_id": sid3, "text": "从上海发往北京，普货 3 吨", "receiver_phone": "12345"})
check("T3.2 手机号非法 -> 拒绝且会话不变",
      c.get("need_input") is True and "receiver_phone" in (c.get("missing") or [])
      and brief_status(sid3) == "blocked", c.get("error") or "")
d3 = detail(sid3)
check("T3.3 两次拒绝均未写 reprocess 事件",
      not any(e.get("type") == "reprocess" for e in (d3.get("events") or [])),
      f"events={[e.get('type') for e in (d3.get('events') or [])]}")

c = req("POST", "/sessions/continue", {})
check("T3.4 缺少 session_id -> 拒绝", bool(c.get("error")), c.get("error") or "")
c = req("POST", "/sessions/continue", {"session_id": "api-doesnotexist"})
check("T3.5 会话不存在 -> 拒绝", bool(c.get("error")), c.get("error") or "")

# ---------------------------------------------------------------------------
print("\n■ T4 并发防重入：同一会话同时只允许一次重处理")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": BLOCK_TEXT})
sid4 = sid_of(r)
CREATED.append(sid4)
results: dict = {}
barrier = threading.Barrier(2)


def _fire(key: str, body: dict):
    barrier.wait()
    results[key] = req("POST", "/sessions/continue", body)


t1 = threading.Thread(target=_fire, args=("a", {"session_id": sid4, "text": REVISED_TEXT}))
t2 = threading.Thread(target=_fire, args=("b", {"session_id": sid4, "text": REVISED_TEXT}))
t1.start(); t2.start(); t1.join(); t2.join()
# 并发第二个请求必须被拦下（两种合法拦截口径都算通过）：
#   a) 防重入闸口：「正在重新处理中」；b) 状态校验：重处理已将状态置回 running，
#   后续请求看到的状态不再是可重处理的终态。两者都保证不会二次重跑。
ok_msgs = ("正在重新处理中", "无法重新处理")
rejected = [k for k, v in results.items()
            if any(m in str(v.get("error") or "") for m in ok_msgs)]
check("T4.1 并发第二个请求被拦下（不重复重跑）", len(rejected) == 1,
      json.dumps({k: (v.get("error") or v.get("thread_id")) for k, v in results.items()},
                 ensure_ascii=False))
# 终止 sid4（若已解锁则处于审核挂起；驳回不推钉钉）
if brief_status(sid4) == "awaiting_review":
    req("POST", "/resume", {"thread_id": sid4, "decision": {"approved": False, "by": "test"}})

# ---------------------------------------------------------------------------
print("\n■ 清理：删除本测试创建的会话")
# ---------------------------------------------------------------------------
for s in CREATED:
    req("POST", "/sessions/delete", {"id": s})
CREATED.clear()
total1 = cur_total()
check("CLEAN.1 测试会话已全部删除，总数复原", total1 == total0, f"total={total0} -> {total1}")

print("\n" + "=" * 74)
print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  [FAIL]", f)
    sys.exit(1)
print("全部通过 ✅")
