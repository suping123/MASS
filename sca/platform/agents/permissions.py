"""每个 Agent 能碰哪些服务（工具权限白名单）。

判定"独立权限"不看文档，看 import：

* 承运商不得 import ``reassign`` —— 它不能给自己改派；
* 异常域不得 import ``pricing`` / ``carrier_ops`` —— 它的判断里不能出现钱，
  否则"这单利润薄，定级从轻"就只是没人写而已，不是不可能发生；
* 货主不得 import ``carrier_ops`` —— 它不能替承运商接单；
* 调度不得 import ``carrier_ops.accept`` —— 成交必须由承运商 agent 自己执行。

**强度如实**：这是 import 级白名单 + ``check_isolation.py`` 的 AST 扫描，
防的是自己人写代码手滑，防不住有人另起 Session 直连表。真正的隔离
要靠数据库账号权限——这一点不能对用户含糊。
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, Iterable, List, Tuple

KINDS = ("shipper", "dispatch", "carrier", "exception")

ALLOWED_SERVICES: Dict[str, FrozenSet[str]] = {
    "shipper": frozenset({
        "orders", "scheduling", "notify", "deadline", "tender", "audit", "advice",
    }),
    "dispatch": frozenset({
        "orders", "scheduling", "triage", "reroute", "deadline", "reassign",
        "notify", "tender", "tender_ops", "audit", "advice",
        # 调度是平台方：挂牌要算两侧服务费、要读当前费率。
        # 这是它的职责，不是越权——它不能用的是 carrier_ops（替承运商签字）。
        "pricing", "runtime",
    }),
    "carrier": frozenset({
        "orders", "carrier_ops", "notify", "tender", "tender_ops", "audit",
        "advice",
        # runtime 给的是**自己这一侧**的费率：承运商要算"扣完服务费还剩多少"，
        # 这是它自己的钱，不是别人的信息。
        "runtime",
    }),
    "exception": frozenset({
        "orders", "triage", "reroute", "reassign", "notify", "tender", "audit",
        # weather_watch：在途天气巡查。它只建 E-WX-* 异常单，不带任何金额，
        # 属于异常域的"眼睛"，不是商业判断——所以给它，但不给它 pricing。
        "weather_watch",
    }),
}

# 文件 → 角色。agent（对话入口）与 daemon（自主循环）用同一套权限：
# 同一个角色不该因为入口不同就有两种权限，否则"对话能做的事"会绕过闸门。
_NAME_RE = re.compile(r"^(shipper|dispatch|carrier|exception)_(agent|daemon)\.py$")


def kind_of(filename: str) -> str | None:
    """``carrier_daemon.py`` → ``carrier``；不是 agent/daemon 文件返回 None。"""
    m = _NAME_RE.match(filename)
    return m.group(1) if m else None


def allowed_for(kind: str) -> FrozenSet[str]:
    return ALLOWED_SERVICES.get(kind, frozenset())


def check(kind: str, services: Iterable[str]) -> Tuple[bool, List[str]]:
    """返回 ``(是否合规, 越权服务名列表)``。纯函数，便于测试。"""
    allowed = allowed_for(kind)
    bad = sorted({s for s in services if s and s not in allowed})
    return (not bad, bad)


def explain(kind: str, bad: List[str]) -> str:
    """给违规写清"为什么这个角色不能碰"，而不是只说"不允许"。"""
    if not bad:
        return ""
    allow = ", ".join(sorted(allowed_for(kind))) or "（无）"
    return (f"{kind} 不得使用服务 {', '.join(bad)}；"
            f"该角色可用服务：{allow}")
