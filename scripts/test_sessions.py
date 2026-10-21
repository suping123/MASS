"""会话历史端到端自检（真实 HTTP 打本机 API，不 mock）。

覆盖：触发 → HITL 审批 → 调度员审核 → 钉钉下发 → 落盘 → 检索/详情/统计 → 脱敏 → 阻断留痕。

用法：
    python scripts/test_sessions.py            # 默认打 http://127.0.0.1:8000
    python scripts/test_sessions.py 8001       # 指定端口

注意：链路末端会**真实推送 2 条钉钉调度通知**（司机 / 收货方），与
scripts/test_dispatch_review.py 的行为一致；不需要真发时请临时关闭
「流程开关 → 调度员审核后下发」或直接驳回。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = f"http://127.0.0.1:{PORT}"
API = BASE + "/api"
# 每个请求显式超时（C4）。2026-10-15 从 15s 放宽到 120s：应急重规划 replan 现走
# plan_route_smart（S2 约束提取 + S3 高德 + S4 带工具裁决 + S5 解释，三次模型调用），
# 实测单次 resume 12-30s，15s 会误报超时（并非链路故障）。
HTTP_TIMEOUT = 120.0
SESSIONS_FILE = Path(__file__).resolve().parents[1] / "data" / "sessions.json"

PASS, FAIL = [], []


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


print("=" * 74)
print("会话历史自检 ·", API)
print("=" * 74)

# 0) 服务可用
h = req("GET", "/health")
if h.get("_http_error"):
    print(f"[FATAL] /api/health 不可用：{h}. 请先启动服务（start_server.bat）")
    sys.exit(2)
print(f"[info] health: hitl={h.get('hitl_enabled')} model_available={h.get('model_available')}")

# 1) 清空历史（危险操作需显式确认串）
r = req("POST", "/sessions/clear", {"confirm": "CLEAR"})
check("清空需确认：空 confirm 被拒绝", req("POST", "/sessions/clear", {}).get("ok") is False)
check("清空历史（confirm=CLEAR）", r.get("ok") is True, f"cleared={r.get('cleared')}")

# 2) 触发一条 P0 会话（冷链温度超标 → 高危挂起）
text = "从上海发往北京，冷链药品，厢温超标到 8℃，需紧急处理，收货方 13800138000"
t0 = time.time()
tr = req("POST", "/trigger", {"text": text})
tid = tr.get("thread_id")
print(f"[info] trigger: level={tr.get('level')} interrupted={tr.get('interrupted')} tid={tid}")
check("触发返回 thread_id", bool(tid))
check("触发已落会话记录", tr.get("session_persisted") is True or tr.get("interrupted") is True)
check("P0/P1 触发后挂起待审批", bool(tr.get("interrupted")) and tr.get("needs_approval") is not False,
      f"level={tr.get('level')}")

# 3) 列表与详情
lst = req("GET", "/sessions?limit=10")
check("列表能查到该会话", any(s["session_id"] == tid for s in lst.get("sessions", [])),
      f"total={lst.get('total')}")
det = req("GET", f"/sessions/{tid}")
sess = det.get("session") or {}
check("详情可取到完整会话", sess.get("session_id") == tid)
check("挂起态 = awaiting_approval", sess.get("status") == "awaiting_approval",
      f"status={sess.get('status')}")
check("需求原文已脱敏（手机号打码）",
      sess.get("input", {}).get("receiver_phone_masked") == "138****8000",
      str(sess.get("input", {}).get("receiver_phone_masked")))
check("时间线含 trigger + interrupt 事件",
      {e.get("type") for e in sess.get("events", [])} >= {"trigger", "interrupt"},
      str([e.get("type") for e in sess.get("events", [])]))
check("记录了异常与等级", (sess.get("exception") or {}).get("code") is not None,
      json.dumps(sess.get("exception"), ensure_ascii=False))

# 4) 审批通过（可能再进入调度员审核挂起）
rs = req("POST", "/resume", {"thread_id": tid, "decision": {"approved": True, "by": "自检脚本"}})
if rs.get("interrupted"):
    check("审批后进入「待审核下发」", True, rs.get("message", "")[:40])
    det2 = req("GET", f"/sessions/{tid}").get("session", {})
    check("挂起态 = awaiting_review", det2.get("status") == "awaiting_review",
          f"status={det2.get('status')}")
    rs = req("POST", "/resume", {"thread_id": tid, "decision": {"approved": True, "by": "调度员"}})
check("恢复后不再挂起", not rs.get("interrupted"), rs.get("error", "")[:80])

# 5) 终态：done + 通知回执 + 摘要
final = req("GET", f"/sessions/{tid}").get("session", {})
print(f"[info] 终态: status={final.get('status')} notify={final.get('notify_sent')}/{final.get('notify_total')} "
      f"duration={final.get('duration_ms')}ms")
check("终态 = done", final.get("status") == "done", f"status={final.get('status')}")
check("记录了耗时", (final.get("duration_ms") or 0) > 0)
check("通知回执已入历史", (final.get("notify_total") or 0) >= 2,
      f"sent={final.get('notify_sent')}/{final.get('notify_total')}")
check("通知回执手机号脱敏",
      all(("13800138000" not in json.dumps(n, ensure_ascii=False))
          for n in final.get("notifications", [])))
check("时间线含 notify 事件",
      any(e.get("type") == "notify" for e in final.get("events", [])))
check("已记录调度摘要（承运商/里程/ETA）",
      any(final.get("summary", {}).get(k) for k in ("carrier", "distance_km", "eta_min")),
      json.dumps(final.get("summary", {}), ensure_ascii=False)[:120])
check("已记录 MCP 工具证据", (len(final.get("mcp_calls") or [])) > 0,
      f"n={len(final.get('mcp_calls') or [])}")
check("已记录意图识别来源", (final.get("intent") or {}).get("source") in ("rule", "model"),
      json.dumps(final.get("intent", {}), ensure_ascii=False)[:100])

# 6) 统计
st = req("GET", "/sessions/stats")
check("统计：总数 ≥ 1", (st.get("total") or 0) >= 1, json.dumps(st.get("by_status"), ensure_ascii=False))
check("统计：通知成功率已计算", st.get("notify_success_rate") is not None or st.get("notify_total") == 0,
      f"rate={st.get('notify_success_rate')}")
check("统计：平均耗时 > 0", (st.get("avg_duration_ms") or 0) > 0)

# 7) 受理阻断也要留痕
blk = req("POST", "/trigger", {})
btid = blk.get("thread_id")
check("空需求被阻断且返回 thread_id", bool(blk.get("error")) and bool(btid),
      (blk.get("error") or "")[:40])
if btid:
    b = req("GET", f"/sessions/{btid}").get("session", {})
    check("阻断场景落 status=blocked", b.get("status") == "blocked", f"status={b.get('status')}")
    f2 = req("GET", "/sessions?status=blocked&limit=10")
    check("按状态筛选可查到阻断记录",
          any(s["session_id"] == btid for s in f2.get("sessions", [])),
          f"total={f2.get('total')}")

# 8) 关键词检索
kw = req("GET", "/sessions?q=" + urllib.parse.quote("冷链"))
check("关键词检索命中", any(s["session_id"] == tid for s in kw.get("sessions", [])),
      f"total={kw.get('total')}")

# 9) 落盘文件不含明文手机号（脱敏硬约束）
if SESSIONS_FILE.is_file():
    raw = SESSIONS_FILE.read_text(encoding="utf-8")
    check("落盘文件无明文手机号", "13800138000" not in raw)
    check("落盘文件无 API Key/Secret", ("sk-" not in raw) and ("SEC" not in raw))
else:
    check("落盘文件存在", False, str(SESSIONS_FILE))

# 10) 单条删除
if btid:
    d = req("POST", "/sessions/delete", {"id": btid})
    check("删除单条会话", d.get("ok") is True)
    check("删除后查不到", req("GET", f"/sessions/{btid}").get("error") is not None)

print("-" * 74)
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}   用时 {time.time() - t0:.1f}s")
if FAIL:
    print("失败项：" + "、".join(FAIL))
print("=" * 74)
sys.exit(1 if FAIL else 0)
