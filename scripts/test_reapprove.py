"""「重新审批」端到端自检（真实 HTTP 打本机 API，不 mock）。

覆盖需求：调度员把调度结果驳回（rejected）后，**旧会话要能重新审批**——
不是整场重跑，而是把会话打回当初的挂起点，调度员在控制台重新通过/驳回，
从原中断点继续（下发通知 -> 复盘 -> 落库），会话 ID / thread_id 全程不变。

  1) T1 全生命周期：P1 触发 -> 审批通过 -> 调度审核驳回(rejected)
     -> **重新审批**（同 ID 打回待审核）-> 控制台批准 -> done；
     全程 thread_id 不变、不产生新会话、通知恰好 2 条（证明从原中断点继续、
     旧检查点已清、无重复推送）；
  2) T2 可反复：重新审批后**再次驳回** -> 又 rejected -> 再重新审批 -> 批准 -> done；
  3) T3 边界：缺 session_id / 会话不存在 / 挂起态（提示走 /resume）/
     done（拒绝）/ blocked（提示改用重新处理）/ 重复重新审批（拒绝）；
  4) T4 不变量：全程会话总数只增本测试自建的条数；时间线含 reapprove 事件。

用法：
    python scripts/test_reapprove.py            # 默认打 http://127.0.0.1:8000
    python scripts/test_reapprove.py 8010       # 指定端口

注意：T1/T2 走完完整生命周期会**真实推送钉钉调度通知**（与 scripts/test_sessions.py、
scripts/test_continue.py 行为一致）；不需要真发时请临时关闭
「流程开关 -> 调度员审核后下发」或把最后的审核改为驳回。
测试自建自删会话，不动历史里已有的演示数据。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = f"http://127.0.0.1:{PORT}"
API = BASE + "/api"
HTTP_TIMEOUT = 120.0         # 图执行 + HITL 多轮 + 重新审批回退 + 模型化重规划，放宽超时

PASS, FAIL = [], []
CREATED: list = []           # 本测试创建的会话 id，收尾统一删除

P1_TEXT = "从广州发往成都的冷藏疫苗，运输途中制冷机组故障"   # 命中 E-CAP-BREAK / P1


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
    """预期总数 = 初始总数 + 本测试已创建且未删除的会话数（重新审批本身不得新增）。"""
    return (total0 or 0) + len(CREATED)


def brief_status(sid: str) -> str:
    return (req("GET", "/sessions/" + sid).get("session") or {}).get("status") or ""


def detail(sid: str) -> dict:
    return (req("GET", "/sessions/" + sid).get("session") or {})


print("=" * 74)
print("重新审批（打回挂起 · 从原中断点继续）自检 ·", API)
print("=" * 74)

# 0) 服务可用
h = req("GET", "/health")
if h.get("_http_error") or h.get("_conn_error"):
    print(f"[FATAL] /api/health 不可用：{h}. 请先启动服务（start_server.bat）")
    sys.exit(2)
print(f"[info] health: hitl={h.get('hitl_enabled')} model_available={h.get('model_available')}")

total0 = cur_total()
print(f"[info] 当前会话总数：{total0}")

# ---------------------------------------------------------------------------
print("\n■ T1 全生命周期：审核驳回 -> 重新审批（同 ID 打回挂起）-> 控制台批准 -> done")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid = sid_of(r)
CREATED.append(sid)
check("T1.1 触发 P1 并挂起审批", bool(sid) and r.get("interrupted") is True,
      f"thread_id={sid}")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": True, "by": "test"}})
check("T1.2 审批通过 -> 进入调度结果审核", r.get("interrupted") is True
      and (r.get("interrupt") or {}).get("type") == "dispatch_review")

r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": False, "by": "test"}})
check("T1.3 调度员驳回 -> 会话 rejected", r.get("interrupted") is False
      and brief_status(sid) == "rejected", f"status={brief_status(sid)}")

# —— 关键：重新审批（不重跑状态机，打回原中断点）——
ra = req("POST", "/sessions/reapprove", {"session_id": sid})
check("T1.4 重新审批返回同一会话 ID（thread_id 不变）",
      sid_of(ra) == sid and ra.get("reapproved") is True,
      f"thread_id={ra.get('thread_id')} phase={ra.get('phase')} "
      f"checkpoint_restored={ra.get('checkpoint_restored')}")
check("T1.5 打回待审核下发且带回原中断载荷",
      ra.get("phase") == "review" and ra.get("status") == "awaiting_review"
      and ra.get("interrupted") is True
      and (ra.get("interrupt") or {}).get("type") == "dispatch_review",
      f"status={ra.get('status')} interrupt={json.dumps(ra.get('interrupt'), ensure_ascii=False)[:80]}")
d = detail(sid)
check("T1.6 落盘状态为 awaiting_review 且无续接指针",
      d.get("status") == "awaiting_review" and not d.get("continued_by")
      and not d.get("parent_session_id"), f"status={d.get('status')}")
ev_types = [e.get("type") for e in (d.get("events") or [])]
check("T1.7 时间线含 reapprove 事件", "reapprove" in ev_types, f"events={ev_types}")

# 控制台重新批准 -> 从原中断点继续
r = req("POST", "/resume", {"thread_id": sid, "decision": {"approved": True, "by": "test"}})
check("T1.8 控制台批准 -> 处置完成（不再二次挂起）", r.get("interrupted") is False,
      f"thread_id={r.get('thread_id')}")
d = detail(sid)
notis = d.get("notifications") or []
check("T1.9 终态 done 且通知恰好 2 条（从原中断点继续，无重复推送）",
      d.get("status") == "done" and len(notis) == 2 and all(n.get("sent") for n in notis),
      f"status={d.get('status')} notifications={len(notis)}")
check("T1.10 重新审批全程未产生新会话（总数 = 初始 + 本测试自建）",
      cur_total() == expect_total(), f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ T2 可反复：重新审批后再驳回 -> 再重新审批 -> 批准 -> done")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid2 = sid_of(r)
CREATED.append(sid2)
req("POST", "/resume", {"thread_id": sid2, "decision": {"approved": True, "by": "test"}})
req("POST", "/resume", {"thread_id": sid2, "decision": {"approved": False, "by": "test"}})
check("T2.1 第二次会话同样到达 rejected", brief_status(sid2) == "rejected",
      f"status={brief_status(sid2)}")

ra = req("POST", "/sessions/reapprove", {"session_id": sid2})
check("T2.2 再次重新审批成功（同 ID）",
      sid_of(ra) == sid2 and ra.get("reapproved") is True, f"thread_id={ra.get('thread_id')}")
# 调度员改主意 -> 再次驳回
r = req("POST", "/resume", {"thread_id": sid2, "decision": {"approved": False, "by": "test"}})
check("T2.3 重新审批后又驳回 -> 回到 rejected",
      r.get("interrupted") is False and brief_status(sid2) == "rejected",
      f"status={brief_status(sid2)}")
ra = req("POST", "/sessions/reapprove", {"session_id": sid2})
check("T2.4 rejected 后可再次重新审批（反复改结论）",
      sid_of(ra) == sid2 and ra.get("reapproved") is True, f"thread_id={ra.get('thread_id')}")
r = req("POST", "/resume", {"thread_id": sid2, "decision": {"approved": True, "by": "test"}})
d2 = detail(sid2)
check("T2.5 最终批准 -> done 且通知恰好 2 条",
      d2.get("status") == "done" and len(d2.get("notifications") or []) == 2,
      f"status={d2.get('status')} notifications={len(d2.get('notifications') or [])}")
ev2 = [e.get("type") for e in (d2.get("events") or [])]
check("T2.6 时间线有两次 reapprove（反复改结论可回放）",
      ev2.count("reapprove") == 2, f"events={ev2}")
check("T2.7 反复重新审批全程未产生新会话",
      cur_total() == expect_total(), f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ T3 边界：非 rejected 状态 / 缺参 / 不存在 / 重复重新审批")
# ---------------------------------------------------------------------------
# 3a) 挂起态：提示走控制台 /resume
r = req("POST", "/trigger", {"text": P1_TEXT})
sid3 = sid_of(r)
CREATED.append(sid3)
ra = req("POST", "/sessions/reapprove", {"session_id": sid3})
check("T3.1 挂起态（awaiting_approval）拒绝重新审批并提示走 /resume",
      bool(ra.get("error")) and ra.get("still_live") is True, ra.get("error") or "")
check("T3.2 挂起态会话状态未被改动", brief_status(sid3) == "awaiting_approval",
      f"status={brief_status(sid3)}")
# 收尾 sid3：审批通过 -> 审核驳回 -> done 会推 2 条钉钉，改为再次驳回终止即可
req("POST", "/resume", {"thread_id": sid3, "decision": {"approved": True, "by": "test"}})
req("POST", "/resume", {"thread_id": sid3, "decision": {"approved": False, "by": "test"}})

# 3b) done：拒绝
ra = req("POST", "/sessions/reapprove", {"session_id": sid2})
check("T3.3 done 会话拒绝重新审批", bool(ra.get("error")), ra.get("error") or "")

# 3c) blocked：提示改用重新处理
r = req("POST", "/trigger", {"text": "帮我安排一批货物运输"})
sid4 = sid_of(r)
CREATED.append(sid4)
check("T3.4 构造 blocked 会话", brief_status(sid4) == "blocked", f"status={brief_status(sid4)}")
ra = req("POST", "/sessions/reapprove", {"session_id": sid4})
check("T3.5 blocked 会话拒绝重新审批并提示改用重新处理",
      bool(ra.get("error")) and ra.get("fallback") == "reprocess", ra.get("error") or "")
check("T3.6 blocked 会话状态未被改动", brief_status(sid4) == "blocked",
      f"status={brief_status(sid4)}")

# 3d) 缺参 / 不存在
ra = req("POST", "/sessions/reapprove", {})
check("T3.7 缺少 session_id -> 拒绝", bool(ra.get("error")), ra.get("error") or "")
ra = req("POST", "/sessions/reapprove", {"session_id": "api-doesnotexist"})
check("T3.8 会话不存在 -> 拒绝", bool(ra.get("error")), ra.get("error") or "")

# 3e) 重复重新审批（已打回挂起）-> 拒绝
#     sid3 在 3a 收尾时被审核驳回，当前是 rejected：先重新审批打回挂起，再点一次应被拦
ra1 = req("POST", "/sessions/reapprove", {"session_id": sid3})
check("T3.9a sid3（rejected）重新审批成功打回挂起",
      ra1.get("reapproved") is True and ra1.get("status") == "awaiting_review",
      f"status={ra1.get('status')}")
ra2 = req("POST", "/sessions/reapprove", {"session_id": sid3})
check("T3.9 已打回挂起的会话再次重新审批被拒（提示走 /resume）",
      bool(ra2.get("error")) and ra2.get("still_live") is True, ra2.get("error") or "")
# 收尾 sid3：审核驳回终止（不推钉钉），删除时一并清掉
req("POST", "/resume", {"thread_id": sid3, "decision": {"approved": False, "by": "test"}})
# 收尾 sid（T1 的会话已 done，无需处理）

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
