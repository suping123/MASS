"""信息边界静态检查（多 Agent 架构的闸门）。

检查五件事：
  1. 各角色 Agent 的代码里不得出现**别的角色的私有字段**；
  2. Agent 之间不得直接 import 对方实现（必须走 `bus.send` 发消息）；
  3. Agent 读订单必须走 `views.load_order`，直接 `db.get(ShipperOrder)` 需显式豁免；
  4. Agent 不得 import 越权服务（`carrier` 不得拿 `reassign` / `pricing`，
     `exception` 不得碰任何跟钱有关的服务）；
  5. Agent 不得**调用**越权函数（承运商能 import `tender_ops` 去投标，
     但不能在同一个模块里调 `close_tender` 给自己定标）；
  6. **路由层不得直接执行成交类写操作**（S5）。`accept_core` / `publish_order` /
     `submit_bid` / `create_order` 只能由 daemon 调用；路由里出现就等于又回到
     "HTTP 直接成交"，四个 agent 随时可以被绕过，第 5 条检验悄悄失效。

为什么用 AST 而不是 grep
------------------------
grep 会把注释和 docstring 一起算进去——而"这里刻意不读 budget_max"这种注释
恰恰应该被允许。AST 天然不含注释，还能拿到 docstring 单独排除，误报少得多。

退出码：0 通过，1 有违规。可直接挂进 smoke 脚本或 CI。
"""
from __future__ import annotations

import ast
import pathlib
import sys
from typing import Dict, List, Optional, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sca.platform.agents import permissions  # noqa: E402

AGENTS_DIR = ROOT / "sca" / "platform" / "agents"

# 每个角色不得触碰的字段（来源：views.ROLE_FIELDS 的补集，人工挑选出真正敏感的）
FORBIDDEN: Dict[str, Set[str]] = {
    # 承运商不得看货主预算、货主身份、原始下单描述（原文可能写了预算）
    "carrier_agent.py": {"budget_max", "gross_price", "shipper_fee",
                         "shipper_org_id", "shipper_user_id"},
    # 货主不得看承运商的成本结构（成本、利润、空驶成本）
    "shipper_agent.py": {"cost_per_km", "quoted_price", "est_profit",
                         "linehaul_cost", "deadhead_cost", "est_cost",
                         "est_fee"},
    # 异常域不得看任何商业信息，否则定级会被"这单值不值得"污染
    "exception_agent.py": {"budget_max", "quoted_price", "est_profit",
                           "est_cost", "cost_est", "contact_phone",
                           "gross_price"},
}

AGENT_MODULES = {"shipper_agent", "carrier_agent", "dispatch_agent",
                 "exception_agent"}

# 每个角色不得调用的**函数**。import 白名单只能管到"能不能拿到这个模块"，
# 管不到"拿到之后调哪个函数"：承运商现在可以 import tender_ops（它要投标），
# 但同一个模块里还有 close_tender / award_by_accept——那是裁决权。
# 不把这一层也钉住，"承运商给自己定标"就只是一行代码的距离。
FORBIDDEN_CALLS: Dict[str, Set[str]] = {
    "carrier": {"close_tender", "open_tender", "award_by_accept",
                "fail_negotiation", "resolve", "reassign", "publish_order",
                "create_plan", "expire_stale"},
    "exception": {"submit_bid", "close_tender", "open_tender",
                  "award_by_accept", "accept", "accept_core", "quote",
                  "publish_order", "resolve"},
    "shipper": {"accept", "accept_core", "quote", "submit_bid", "close_tender",
                "open_tender", "resolve", "reassign", "publish_order"},
    # 调度可以裁决、可以挂牌，但不能替承运商签字：accept / accept_core /
    # quote / submit_bid 是承运商的动作，调度一调，"多方"就退回成"一方代办"。
    "dispatch": {"accept", "accept_core", "quote", "submit_bid", "carrier_quote"},
}

# 成交类写操作的**执行体**。S5 起它们只该出现在 daemons/ 里。
# 路由是"人类闸口"，只投 invoke + 等回复；出现直接调用就是旁路。
WRITE_EXECUTORS: Set[str] = {"accept_core", "publish_order", "submit_bid",
                             "create_order"}
ROUTERS_DIR = ROOT / "sca" / "platform" / "routers"

EXEMPT_MARK = "noqa: isolation"


def _docstrings(tree: ast.AST) -> Set[int]:
    """收集模块/类/函数的 docstring 节点 id，检查时跳过。"""
    out: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _service_names(node: ast.ImportFrom) -> List[str]:
    """从 import 语句里取出 ``..services.<name>`` 的 ``<name>``。

    三种写法都要认：
      ``from ..services import advice, carrier_ops``   → 模块名就是 services，取 names
      ``from ..services.orders import transition``     → 取 services 的下一段
      ``from sca.platform.services import x``          → 同上
    """
    mod = node.module or ""
    parts = [p for p in mod.split(".") if p]
    if "services" in parts:
        i = parts.index("services")
        if i + 1 < len(parts):
            return [parts[i + 1]]
        return [a.name for a in node.names]
    return []


def check_file(path: pathlib.Path) -> List[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [f"{path.name}: 语法错误 {exc}"]

    banned = FORBIDDEN.get(path.name, set())
    kind = permissions.kind_of(path.name)
    # daemon 与 agent 是同一个角色的两个入口：同一个角色不该因为换了入口就
    # 能看不同字段。所以 daemons/*_daemon.py 沿用对应 *_agent.py 的字段禁令。
    if not banned and kind:
        banned = FORBIDDEN.get(f"{kind}_agent.py", set())
    skip = _docstrings(tree)
    lines = src.splitlines()
    problems: List[str] = []

    def at(node) -> int:
        return getattr(node, "lineno", 0)

    for node in ast.walk(tree):
        # 1) 私有字段：属性访问 或 字符串常量（dict key / getattr 参数）
        if banned:
            if isinstance(node, ast.Attribute) and node.attr in banned:
                problems.append(f"{path.name}:{at(node)} 越权字段 {node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value in banned and id(node) not in skip:
                problems.append(f"{path.name}:{at(node)} 越权字段（字符串）{node.value}")

        # 2) Agent 之间直接 import
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[-1]
            same_role = kind and mod in (f"{kind}_agent", f"{kind}_daemon")
            if mod in AGENT_MODULES and mod != path.stem and not same_role:
                problems.append(
                    f"{path.name}:{at(node)} 直接 import 另一个 Agent（{mod}），"
                    f"跨 Agent 协作必须走 bus.send 发消息")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in AGENT_MODULES and \
                        a.name.split(".")[-1] != path.stem:
                    problems.append(
                        f"{path.name}:{at(node)} 直接 import 另一个 Agent（{a.name}）")

        # 3) 越权 import 服务（工具权限白名单，见 agents/permissions.py）
        if kind and isinstance(node, ast.ImportFrom):
            svc = _service_names(node)
            if svc:
                good, bad = permissions.check(kind, svc)
                if not good:
                    problems.append(
                        f"{path.name}:{at(node)} 越权 import 服务 "
                        f"{', '.join(bad)} —— {permissions.explain(kind, bad)}")

        # 5) 越权调用（同一模块里的越权函数，import 白名单挡不住）
        if kind and isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Attribute):
            bad_calls = FORBIDDEN_CALLS.get(kind, set())
            if node.func.attr in bad_calls:
                problems.append(
                    f"{path.name}:{at(node)} 越权调用 {node.func.attr}() —— "
                    f"{kind} 不该执行这个动作")

    # 4) 直连 ORM 读订单
    for i, line in enumerate(lines, 1):
        if "db.get(ShipperOrder" not in line:
            continue
        ctx = line + (lines[i - 2] if i >= 2 else "")
        if EXEMPT_MARK in ctx:
            continue
        problems.append(
            f"{path.name}:{i} 直接 db.get(ShipperOrder)，读订单请用 views.load_order")
    return problems


def check_routes(target: Optional[pathlib.Path] = None) -> List[str]:
    """路由层不得直接执行成交类写操作（第 6 条）。

    为什么这条要单独查：路由直接调 `accept_core` 的话，业务流程照样跑得通、
    大部分测试也是绿的（测试打的就是这个接口）——**只有"移除交互"那条测试会红**。
    等有人为了让那条测试变绿而在别处加个兜底，架构就已经退回去了。
    所以宁可在这里硬拦。
    """
    problems: List[str] = []
    root = target or ROUTERS_DIR
    if not root.is_dir():
        return problems
    for p in sorted(root.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as exc:
            problems.append(f"{p.name}: 语法错误 {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in WRITE_EXECUTORS:
                problems.append(
                    f"{p.name}:{getattr(node, 'lineno', 0)} 路由直接调用 "
                    f"{node.func.attr}() —— 成交写操作必须投消息交给 Agent 执行")
    return problems


def main() -> int:
    if not AGENTS_DIR.is_dir():
        print(f"找不到 agents 目录：{AGENTS_DIR}")
        return 1
    targets = sorted(list(AGENTS_DIR.glob("*_agent.py")) +
                     list(AGENTS_DIR.glob("daemons/*_daemon.py")))
    all_problems: List[str] = []
    for p in targets:
        all_problems.extend(check_file(p))
    all_problems.extend(check_routes())

    if not all_problems:
        print(f"[isolation] PASS —— 检查 {len(targets)} 个 Agent 文件 + 路由层，"
              f"未见越权字段、未见越权服务、未见越权调用、未见 Agent 间直接引用、"
              f"未见路由绕过 Agent 直接成交")
        return 0
    print(f"[isolation] FAIL —— {len(all_problems)} 处违规：")
    for msg in all_problems:
        print("  -", msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
