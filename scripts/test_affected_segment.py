"""受影响路段（可选录入）与应急重规划跳过模型 的端到端专项测试。

覆盖三件事（2026-10-15 用户需求）：
  ① 未提供具体受影响路段 -> 应急重规划**跳过模型裁决**（model_decision.source=skipped），
     不白花 12-30s 的模型调用，并如实说明"维持原走廊"；
  ② 提供受影响路段（可选输入框）-> 模型介入做**局部**绕行（source=model），
     改道增量按同口径（高德途经点链）求，展示里程 = 直连里程 + 真实绕行；
  ③ 路线规划结果**具体到县级**（途经点为连续覆盖的县级节点，不再是省会级）。

用法（项目根目录，必须用 .venv；默认端口 8000）：
  .venv\\Scripts\\python.exe scripts/test_affected_segment.py [port]
"""
import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = "http://127.0.0.1:%s" % PORT
HTTP_TIMEOUT = 180.0     # 含模型化应急重规划（12-30s/次），放宽超时

_fail = []
_PASSED = [0]


def check(cond, label, extra=""):
    ok = bool(cond)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           (" — " + str(extra)) if extra else ""))
    if ok:
        _PASSED[0] += 1
    else:
        _fail.append(label)


def post(path, body, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=60.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def drain(tid, rounds=3):
    """把挂在 dispatch_review 的会话批准到终态，避免留挂起现场。"""
    for _ in range(rounds):
        try:
            out = post("/api/resume", {"thread_id": tid,
                                       "decision": {"approved": True, "by": "seg-test"}})
        except Exception as exc:  # noqa
            print("  (resume 失败：%s)" % exc)
            return
        if not out.get("interrupted"):
            return


def plan_of(r):
    return r.get("plan") or {}


print("=" * 74)
print("A) 常规调度单：途经点应具体到县级（连续覆盖，不再是省会级）")
print("=" * 74)
r_a = post("/api/trigger", {"text": "石家庄到南京 普通货物 20t 明日下午3点前到",
                            "user_id": "seg-test"})
p_a = plan_of(r_a)
wp_a = p_a.get("waypoints") or []
print("  等级=%s 异常=%s 里程=%s provider=%s" %
      (r_a.get("level"), (r_a.get("exception") or {}).get("name"),
       p_a.get("distance_km"), p_a.get("provider")))
print("  途经点(%d): %s" % (len(wp_a), " → ".join(wp_a)))
check(len(wp_a) >= 10, "途经点数量 ≥10（县级粒度）", len(wp_a))
check(p_a.get("provider", "").startswith("amap"), "使用高德真实路径", p_a.get("provider"))
# 县级覆盖：相邻途经点不应出现超长空档（除起讫首尾段外）
check("清河" in wp_a or "夏津" in wp_a, "河北段出现县级节点（清河/夏津）")
check("泰安" in wp_a and "曲阜" in wp_a, "山东段出现县级节点（泰安/曲阜）")
drain(r_a.get("thread_id"))

print()
print("=" * 74)
print("B) 异常但未给受影响路段：应跳过模型（source=skipped）且明显更快")
print("=" * 74)
t0 = time.perf_counter()
r_b = post("/api/trigger", {"text": "石家庄到南京 危险品 20t 前方拥堵严重",
                            "user_id": "seg-test"})
sec_b = time.perf_counter() - t0
p_b = plan_of(r_b)
md_b = p_b.get("model_decision") or {}
print("  耗时=%.1fs 等级=%s 异常=%s 里程=%s" %
      (sec_b, r_b.get("level"), (r_b.get("exception") or {}).get("name"),
       p_b.get("distance_km")))
print("  model_decision.source=%s  reroute=%s  skip_reason=%s" %
      (md_b.get("source"), p_b.get("reroute"), p_b.get("skip_reason")))
check(md_b.get("source") == "skipped", "跳过模型裁决（source=skipped）", md_b.get("source"))
check(p_b.get("reroute") is False, "未宣称产生实际改道")
check(bool(p_b.get("skip_reason")), "给出跳过原因", p_b.get("skip_reason"))
check(any("跳过模型" in n for n in (p_b.get("notes") or [])),
      "notes 明确说明跳过模型", p_b.get("notes"))
drain(r_b.get("thread_id"))

print()
print("=" * 74)
print("C) 异常 + 给定受影响路段：模型介入做局部绕行（source=model）")
print("=" * 74)
t0 = time.perf_counter()
r_c = post("/api/trigger", {"text": "石家庄到南京 危险品 20t 前方拥堵严重",
                            "affected_segment": "微山湖特大桥", "user_id": "seg-test"})
sec_c = time.perf_counter() - t0
p_c = plan_of(r_c)
md_c = p_c.get("model_decision") or {}
prev_c = p_c.get("previous") or {}
print("  耗时=%.1fs 里程=%s provider=%s 绕行=%s" %
      (sec_c, p_c.get("distance_km"), p_c.get("provider"), p_c.get("detour_km")))
print("  model_decision.source=%s  action=%s  avoid_cities=%s" %
      (md_c.get("source"), md_c.get("decision"), md_c.get("avoid_cities")))
for n in (p_c.get("notes") or []):
    print("    note:", n)
check(r_c.get("affected_segment") == "微山湖特大桥", "响应回显受影响路段")
check(md_c.get("source") == "model", "模型真实介入（source=model）", md_c.get("source"))
check(bool(md_c.get("avoid_cities")), "模型给出规避城市", md_c.get("avoid_cities"))
check(p_c.get("provider") == "amap(waypoints)", "改道后里程同源（高德途经点重算）",
      p_c.get("provider"))
check(prev_c.get("distance_km") and p_c.get("distance_km"),
      "保留了改道前基线（供对比）", (prev_c.get("distance_km"), p_c.get("distance_km")))
check(sec_c > sec_b, "给了路段才付模型耗时（C 慢于 B）", (round(sec_b, 1), round(sec_c, 1)))
check(any("同口径" in n for n in (p_c.get("notes") or [])),
      "绕行增量按同口径求得（不虚报）", p_c.get("notes"))
drain(r_c.get("thread_id"))

print()
print("=" * 74)
print("D) 受影响路段落盘与会话详情可查")
print("=" * 74)
rec = get("/api/sessions/%s" % r_c.get("thread_id")).get("session") or {}
check((rec.get("input") or {}).get("affected_segment") == "微山湖特大桥",
      "会话 input.affected_segment 已记录", (rec.get("input") or {}).get("affected_segment"))

print()
print("=" * 74)
print("结果：PASS %d / FAIL %d" % (_PASSED[0], len(_fail)))
if _fail:
    for f in _fail:
        print("   -", f)
    sys.exit(1)
print("全部通过 ✅")
