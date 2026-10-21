"""Agent 间消息契约。

多 Agent 的"通信"必须是有类型的，否则就退化成函数调用。这里定义 7 种消息，
以及每种消息**必须携带**的字段。任何 Agent 发消息前先过 `validate()`，
缺字段直接拒绝——比事后查日志便宜。

消息类型与其存在的理由
----------------------
propose  : 提出方案/报价/改期建议。承载"我愿意这么做"。
counter  : 对 propose 的反提议。**这是多 Agent 与单 Agent 的分水岭**：
           如果永远没人 counter，说明各方目标一致，那就该合并成一个 Agent。
accept   : 接受对端提议。
reject   : 拒绝，必须给理由（无理由的拒绝无法复盘）。
request  : 索取信息。跨域取数只能走这个，不能直接读对方记忆。
escalate : 升级给人。Agent 判断超出自己权限（改派、超权限加价、契约冲突）时用。
notify   : 结果通知，不需要对方响应。
invoke   : **人类闸口委托一个写操作**（S5）。见下。

为什么需要 `invoke`，以及 `human` 这个身份
-------------------------------------------
下单/挂牌/投标/接单这些写操作，发起者是人（点了按钮、对话里说了"确认"），
执行者是某个 Agent 进程。人不属于任何 Agent，所以不能用 `dispatch -> carrier`
之类的边来伪装——**"调度员替你接单"是假的**，事后复盘看到的是错的人。

于是引入 `human:{user_id}`：它只代表人，不是第五个 Agent（没有循环、没有收件箱）。
人类只能对它自己那一侧的 Agent 发 `invoke`，Agent 也能回它（HTTP 请求等回复）。
这条边是**单向能力的入口**，不是 Agent 之间的通信；Agent 之间仍然只能走
propose/counter/accept/reject/request/escalate/notify。

为什么不让人直接调服务函数：那样"移除交互"就成了空话——关掉所有 Agent 进程，
订单照样能成交，那四个进程就只是摆设。

thread_id 约定
--------------
`order:{order_id}` 或 `exc:{exception_id}`。同一笔业务的全部往复挂在一个 thread 下，
`parent_id` 指回被回应的那条，形成可回放的对话树。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable

# 消息类型 -> 必填载荷字段
SCHEMA: Dict[str, tuple] = {
    "propose":  ("amount", "reason"),          # amount 可为 None（非价格类提议）
    "counter":  ("amount", "reason"),
    "accept":   (),
    "reject":   ("reason",),
    "request":  ("fields",),                   # 要哪些字段，必须显式列出
    "escalate": ("reason", "ask"),             # ask = 要人做什么决定
    "notify":   ("event",),
    # action = 要执行的写操作名（白名单在 daemon 的 handler 里），args = 它的入参。
    # 两个字段都必填：允许"省略入参"就等于允许 Agent 猜，猜出来的单子是错的。
    "invoke":   ("action", "args"),
}

TYPES = tuple(SCHEMA)

# 人的身份前缀。它不是一个 Agent：没有循环、没有收件箱、不注册 AgentActor。
HUMAN_KIND = "human"

# 谁可以向谁发（白名单）。刻意不给"任意对任意"——角色边界不是口号。
ALLOWED_EDGES = {
    ("shipper", "dispatch"),
    ("dispatch", "shipper"),
    ("dispatch", "carrier"),
    ("carrier", "dispatch"),
    ("carrier", "exception"),
    ("dispatch", "exception"),
    ("exception", "dispatch"),
    ("exception", "carrier"),
    ("exception", "shipper"),
    ("shipper", "exception"),
}
# 人类闸口：人只能对它那一侧的 Agent 委托，Agent 只能回人类。四个方向都显式列出，
# 不用"human 例外处理"——例外一多，白名单就读不出边界了。
ALLOWED_EDGES |= {(HUMAN_KIND, k) for k in ("shipper", "dispatch", "carrier", "exception")}
ALLOWED_EDGES |= {(k, HUMAN_KIND) for k in ("shipper", "dispatch", "carrier", "exception")}

# 这些动作**不允许**由 Agent 之间自行完成，必须落到人审闸口。
# 列在这里是为了让"自动化边界"变成可读的常量，而不是散落在各处的 if。
HUMAN_REQUIRED = frozenset({
    "reassign",        # 换承运商
    "extend",          # 改承诺时效
    "price_increase",  # 超权限加价
    "cancel",          # 取消订单
})


class ContractError(ValueError):
    """消息不合契约。"""


def kind_of(agent_key: str) -> str:
    """`carrier:12` -> `carrier`；`dispatch` -> `dispatch`。"""
    return (agent_key or "").split(":", 1)[0]


def validate(from_agent: str, to_agent: str, msg_type: str,
             payload: Dict[str, Any] | None) -> Dict[str, Any]:
    """校验并返回清洗后的载荷。不合契约直接抛 ContractError。"""
    if msg_type not in SCHEMA:
        raise ContractError(f"未知消息类型 {msg_type!r}，合法值：{'/'.join(TYPES)}")
    fk, tk = kind_of(from_agent), kind_of(to_agent)
    if fk == tk:
        raise ContractError(f"不能给自己发消息（{fk}）")
    if (fk, tk) not in ALLOWED_EDGES:
        raise ContractError(f"不允许的通信方向：{fk} -> {tk}")

    p = dict(payload or {})
    missing = [k for k in SCHEMA[msg_type] if k not in p]
    if missing:
        raise ContractError(f"{msg_type} 缺少必填字段：{', '.join(missing)}")
    if msg_type == "request":
        fields = p.get("fields")
        if not isinstance(fields, (list, tuple)) or not fields:
            raise ContractError("request.fields 必须是非空数组")
        p["fields"] = [str(x) for x in fields]
    if msg_type in ("propose", "counter") and p.get("amount") is not None:
        try:
            p["amount"] = float(p["amount"])
        except (TypeError, ValueError):
            raise ContractError("amount 必须是数字或 null") from None
    if msg_type in ("reject", "escalate") and len(str(p.get("reason") or "")) < 2:
        raise ContractError("reject/escalate 必须写理由（至少 2 个字）")
    if msg_type == "invoke":
        action = str(p.get("action") or "").strip()
        if not action:
            raise ContractError("invoke.action 不能为空")
        if not isinstance(p.get("args"), dict):
            raise ContractError("invoke.args 必须是对象（没有入参就给 {}）")
        p["action"] = action
    return p


def requires_human(action: str) -> bool:
    return action in HUMAN_REQUIRED


def thread_of(order_id: int | None = None,
              exception_id: int | None = None) -> str:
    if exception_id:
        return f"exc:{int(exception_id)}"
    if order_id:
        return f"order:{int(order_id)}"
    raise ValueError("thread_id 需要 order_id 或 exception_id")


def human_key(user_id: Any) -> str:
    """人类的 agent_key。人不是 Agent，但消息需要收发双方都有身份。"""
    return f"{HUMAN_KIND}:{int(user_id)}"


def is_human(agent_key: str) -> bool:
    return kind_of(agent_key) == HUMAN_KIND


def describe(msg_type: str) -> str:
    """给页面/日志用的中文说明。"""
    return {
        "propose": "提出方案", "counter": "反提议", "accept": "接受",
        "reject": "拒绝", "request": "索取信息", "escalate": "升级人工",
        "notify": "结果通知", "invoke": "委托执行",
    }.get(msg_type, msg_type)


__all__ = ["SCHEMA", "TYPES", "ALLOWED_EDGES", "HUMAN_REQUIRED", "HUMAN_KIND",
           "ContractError", "kind_of", "validate", "requires_human", "thread_of",
           "human_key", "is_human", "describe"]
