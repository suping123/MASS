"""「回退到输入重填」端到端自检（真实 HTTP 打本机 API，不 mock）。

覆盖需求：**高危审批驳回**与**处理结果审批驳回**两种场景，都应能直接回退到
输入异常描述的步骤（自动带出该会话信息），在同一会话上从初始规划重新开始：

  R1 高危审批驳回（2026-10-17 起 = 不下发 + 终态 rejected）-> 带 rewind=true 回退
     -> 同 thread_id 原地重跑 -> 重新挂起审批；
  R2 回退时修订需求文本（换起讫地）-> 重新解析 -> 同会话从初始规划重来；
  R3 调度审核挂起中（awaiting_review）-> rewind 回退 -> 同会话重跑
     （通知尚未发出，回退无重复下发副作用）；挂起态不带 rewind 被拒绝（回归保护）；
  R4 审核驳回终态（rejected）+ rewind -> 原有重处理路径不受影响（回归保护）；
  R5 边界：done + rewind 仍拒绝；缺 session_id 拒绝。

不变量（全程断言）：session_id / thread_id 不变、不产生新会话、回退事件落时间线、
旧检查点已清（checkpoint_purged）。

钉钉推送：仅 R5 为验证「done + rewind 仍拒绝」而完整跑完一条生命周期，会**真实推送
2 条调度通知**（司机 / 收货方）；其余断言全部止步于审批/审核闸口，零推送——适合频繁重跑。

用法：
    python scripts/test_rewind.py            # 默认打 http://127.0.0.1:8000
    python scripts/test_rewind.py 8010       # 指定端口
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = f"http://127.0.0.1:{PORT}"
API = BASE + "/api"
HTTP_TIMEOUT = 120.0         # 回退重跑含模型化应急重规划，放宽超时

PASS, FAIL = [], []
CREATED: list = []

P1_TEXT = "从广州发往成都的冷藏疫苗，运输途中制冷机组故障"      # E-CAP-BREAK / P1
P1_REVISED = "从上海发往重庆的冷藏疫苗，运输途中制冷机组故障"    # 同故障谱，换起讫地


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
    except Exception as exc:  # noqa
        return {"_conn_error": str(exc)}


def sid_of(r: dict) -> str:
    return str(r.get("thread_id") or "")


def cur_total() -> int:
    return (req("GET", "/sessions?limit=1") or {}).get("total") or 0


def expect_total() -> int:
    return (total0 or 0) + len(CREATED)


def brief_status(sid: str) -> str:
    return (req("GET", "/sessions/" + sid).get("session") or {}).get("status") or ""


def detail(sid: str) -> dict:
    return (req("GET", "/sessions/" + sid).get("session") or {})


def event_labels(sid: str) -> list:
    return [e.get("label") or "" for e in (detail(sid).get("events") or [])]


def approve(sid: str, ok: bool = True) -> dict:
    return req("POST", "/resume", {"thread_id": sid, "decision": {"approved": ok, "by": "test"}})


print("=" * 74)
print("「回退到输入重填」自检 ·", API)
print("=" * 74)

h = req("GET", "/health")
if h.get("_http_error") or h.get("_conn_error"):
    print(f"[FATAL] /api/health 不可用：{h}. 请先启动服务（start_server.bat）")
    sys.exit(2)
print(f"[info] health: hitl={h.get('hitl_enabled')}")

total0 = (req("GET", "/sessions?limit=1") or {}).get("total")
print(f"[info] 当前会话总数：{total0}")

# ---------------------------------------------------------------------------
print("\n■ R1 高危审批不通过 -> 回退到输入重填（核心场景 A）")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid = sid_of(r)
CREATED.append(sid)
check("R1.1 触发 P1 并挂起审批", bool(sid) and r.get("interrupted") is True,
      f"thread_id={sid}")

# 2026-10-17 语义变更：审批驳回 = 不下发，会话以 rejected 收尾。
# 旧断言（"不终态、绕回闸口再次挂起"）实际固化的是"驳回仍会走到 execute 下发 →
# 处置段再弹一次审批"那处松口，已随 `approval_gate` 出边收口一并废弃。
r = approve(sid, ok=False)
check("R1.2 审批驳回 -> 不下发、不挂起（不把驳回当放行）",
      r.get("interrupted") is False and sid_of(r) == sid,
      f"interrupted={r.get('interrupted')} status={(r.get('order') or {}).get('status')}")
check("R1.3 驳回后未落 TMS 运单",
      (r.get("order") or {}).get("status") == "approval_rejected"
      and not any("create_shipment" in json.dumps(c, ensure_ascii=False)
                  for c in (r.get("mcp_calls") or [])),
      f"status={(r.get('order') or {}).get('status')} "
      f"calls={[c.get('tool') for c in (r.get('mcp_calls') or [])]}")
check("R1.4 落盘终态为 rejected（不是 done）", brief_status(sid) == "rejected",
      f"status={brief_status(sid)}")

c = req("POST", "/sessions/continue", {"session_id": sid, "rewind": True, "text": P1_TEXT})
check("R1.5 驳回后回退：同一会话 ID（thread_id 不变）",
      sid_of(c) == sid and c.get("reprocessed") is True and c.get("checkpoint_purged") is True,
      f"thread_id={c.get('thread_id')} reprocessed={c.get('reprocessed')} "
      f"purged={c.get('checkpoint_purged')}")
check("R1.6 回退后从初始规划重跑，重新挂起审批", c.get("interrupted") is True,
      f"interrupt={json.dumps(c.get('interrupt'), ensure_ascii=False)[:80]}")
labels = event_labels(sid)
check("R1.7 时间线含「回退到输入步骤」事件",
      any("回退到输入步骤" in lb for lb in labels), f"labels={labels[-4:]}")
check("R1.8 全程未产生新会话", cur_total() == expect_total(),
      f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ R2 回退时修订需求（换起讫地）-> 重新解析 -> 同会话重来")
# ---------------------------------------------------------------------------
c = req("POST", "/sessions/continue", {"session_id": sid, "rewind": True, "text": P1_REVISED})
check("R2.1 修订文本 + rewind：仍是同一会话 ID",
      sid_of(c) == sid and c.get("reprocessed") is True and c.get("input_revised") is True,
      f"thread_id={c.get('thread_id')}")
d = detail(sid)
check("R2.2 需求原文已更新为修订版",
      (d.get("input") or {}).get("text") == P1_REVISED,
      f"text={(d.get('input') or {}).get('text')}")
check("R2.3 修订后重新挂起审批（从初始规划重新开始）",
      d.get("status") == "awaiting_approval", f"status={d.get('status')}")

# ---------------------------------------------------------------------------
print("\n■ R3 调度审核挂起中（awaiting_review）-> rewind 回退（场景 B 挂起变体）")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid2 = sid_of(r)
CREATED.append(sid2)
r = approve(sid2, ok=True)
check("R3.1 审批通过 -> 调度结果审核挂起",
      r.get("interrupted") is True and (r.get("interrupt") or {}).get("type") == "dispatch_review",
      f"interrupt={(r.get('interrupt') or {}).get('type')}")
check("R3.2 落盘状态为 awaiting_review", brief_status(sid2) == "awaiting_review",
      f"status={brief_status(sid2)}")

c = req("POST", "/sessions/continue", {"session_id": sid2, "rewind": True})
check("R3.3 rewind 回退：同一会话 ID（复用原运单快照）",
      sid_of(c) == sid2 and c.get("reprocessed") is True and c.get("checkpoint_purged") is True,
      f"thread_id={c.get('thread_id')}")
check("R3.4 回退后从初始规划重跑，重新挂起审批",
      c.get("interrupted") is True and brief_status(sid2) == "awaiting_approval",
      f"status={brief_status(sid2)}")
# 「挂起态不带 rewind 应被拒绝」的回归保护从 R1 挪到这里：R1 现在以 rejected 收尾
# （rejected 本来就该允许原地重处理），只有真挂起的会话才该提示走 /resume。
c2 = req("POST", "/sessions/continue", {"session_id": sid2})
check("R3.5 挂起态不带 rewind 被拒绝（提示走 /resume，回归保护）",
      bool(c2.get("error")) and c2.get("still_live") is True, c2.get("error") or "")
check("R3.6 被拒绝的重处理未动会话状态",
      brief_status(sid2) == "awaiting_approval", f"status={brief_status(sid2)}")
d2 = detail(sid2)
check("R3.7 审核闸口在通知发出之前：全程 0 条通知（无重复下发副作用）",
      len(d2.get("notifications") or []) == 0,
      f"notifications={len(d2.get('notifications') or [])}")
check("R3.8 未产生新会话", cur_total() == expect_total(),
      f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ R4 审核驳回终态（rejected）+ rewind：原路径不受影响（回归保护）")
# ---------------------------------------------------------------------------
r = req("POST", "/trigger", {"text": P1_TEXT})
sid3 = sid_of(r)
CREATED.append(sid3)
approve(sid3, ok=True)
r = approve(sid3, ok=False)
check("R4.1 调度员审核驳回 -> 会话 rejected",
      r.get("interrupted") is False and brief_status(sid3) == "rejected",
      f"status={brief_status(sid3)}")

c = req("POST", "/sessions/continue", {"session_id": sid3, "rewind": True, "text": P1_TEXT})
check("R4.2 rejected + rewind：同一会话 ID 原地重跑（标志不影响终态路径）",
      sid_of(c) == sid3 and c.get("reprocessed") is True,
      f"thread_id={c.get('thread_id')}")
check("R4.3 未产生新会话", cur_total() == expect_total(),
      f"total={cur_total()} expect={expect_total()}")

# ---------------------------------------------------------------------------
print("\n■ R5 边界：done + rewind 仍拒绝；缺参拒绝")
# ---------------------------------------------------------------------------
r = approve(sid3, ok=True)          # 审批通过
r = approve(sid3, ok=True)          # 审核通过 -> done（本测试唯一一次真实下发）
check("R5.1 审核通过 -> done", r.get("interrupted") is False and brief_status(sid3) == "done",
      f"status={brief_status(sid3)}")
c = req("POST", "/sessions/continue", {"session_id": sid3, "rewind": True})
check("R5.2 done + rewind 仍拒绝（避免重复下发）",
      bool(c.get("error")) and "done" in str(c.get("error")), c.get("error") or "")
c = req("POST", "/sessions/continue", {"rewind": True})
check("R5.3 缺 session_id -> 拒绝", bool(c.get("error")), c.get("error") or "")

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
