"""信息边界：每个角色只能看到自己该看的字段。

为什么需要这一层
----------------
改造前三个平台的 Agent 都是直接 `db.get(ShipperOrder, id)` 拿**整行**，靠调用方
自觉不去碰 `budget_max`。这是"约定"，不是"约束"——新增一行代码就可能漏。

这里改成**白名单投影**：字段必须显式列出才可见。新增字段默认对所有人不可见，
必须主动加进 `ROLE_FIELDS` 才会暴露。这比"排除敏感字段"可靠，因为漏写白名单
只会看不到，不会漏出去。

严格程度要说清楚
----------------
这是**工程约束，不是安全边界**。它防的是"自己人写代码时手滑"，防不住能直连
MySQL 的人。生产环境真正的隔离要靠 DB 账号权限（本轮不做）。

用法
----
    from .views import load_order
    order = load_order(db, order_id, "carrier")   # 越权字段访问直接抛 IsolationError

    # 写操作仍走原对象（ORM 需要真实实例）：
    order = db.get(ShipperOrder, order_id)
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from ..models import CarrierBid, OrderPlan, ShipperOrder


class IsolationError(RuntimeError):
    """越权访问跨域私有字段。

    刻意抛异常而不是返回 None：静默降级会让越权代码活到线上，
    而这里一旦报错，测试立刻红，问题就停在提交前。
    """


# ---------------------------------------------------------------------------
# 字段白名单
#
# 判断依据是"这个角色在业务上有没有必要知道"，不是"技术上拿不拿得到"。
# 特别说明 source_text：它是货主下单时的一段自由文本，很可能写着"预算 5000"，
# 因此对承运商与异常域一并屏蔽——按字段名过滤挡不住这种泄漏，只能整段不给。
# ---------------------------------------------------------------------------
_ALL_ORDER = {
    "id", "order_no", "shipper_user_id", "shipper_org_id", "source_text",
    "origin", "dest", "cargo_type", "weight_t", "temp_ctrl", "hazmat",
    "required_by", "required_by_at", "budget_max", "contact_name",
    "contact_phone", "status", "active_plan_id", "carrier_org_id",
    "carrier_user_id", "created_at", "updated_at",
}

ROLE_FIELDS: Dict[str, Dict[str, FrozenSet[str]]] = {
    "shipper": {
        # 货主看自己的单：预算、联系人当然可见；承运商的成本与利润不在这个对象上
        "order": frozenset(_ALL_ORDER),
        "plan": frozenset({"id", "order_id", "version", "route_json", "distance_km",
                           "eta_h", "cost_est", "arbitration", "carrier_org_id",
                           "created_at"}),
        "bid": frozenset({"id", "order_id", "publish_id", "carrier_org_id",
                          "decision", "quoted_price", "decided_at", "created_at"}),
    },
    "carrier": {
        # 承运商：看不到预算、看不到货主身份、看不到原始描述（原文可能含预算）
        "order": frozenset(_ALL_ORDER - {"budget_max", "shipper_user_id",
                                         "shipper_org_id", "source_text"}),
        # 成本估算是平台侧算给调度看的，承运商看到等于看到自己的报价上限
        "plan": frozenset({"id", "order_id", "version", "route_json", "distance_km",
                           "eta_h", "created_at"}),
        # bid 另行按 org_id 过滤"只能看自己的"，字段层面先放开报价相关
        "bid": frozenset({"id", "order_id", "publish_id", "carrier_org_id",
                          "decision", "quoted_price", "est_cost", "est_fee",
                          "est_profit", "linehaul_cost", "deadhead_km",
                          "deadhead_cost", "deadhead_from", "position_text",
                          "decided_at", "created_at"}),
    },
    "dispatch": {
        # 调度是平台方，订单与方案全量可见（承运商成本模型属平台机密，另表存放）
        "order": frozenset(_ALL_ORDER),
        "plan": frozenset({"id", "order_id", "version", "route_json", "distance_km",
                           "eta_h", "cost_est", "arbitration", "carrier_org_id",
                           "created_by_user_id", "created_at"}),
        "bid": frozenset({"id", "order_id", "publish_id", "carrier_org_id",
                          "carrier_user_id", "decision", "quoted_price", "est_cost",
                          "est_fee", "est_profit", "linehaul_cost", "deadhead_km",
                          "deadhead_cost", "deadhead_from", "position_text",
                          "decided_at", "created_at"}),
    },
    "exception": {
        # 异常域只做定级与处置建议，**不该看到任何商业信息**：
        # 一旦它知道这单利润高，定级就可能被"值不值得麻烦"污染。
        "order": frozenset(_ALL_ORDER - {"budget_max", "contact_name",
                                         "contact_phone", "shipper_user_id",
                                         "shipper_org_id", "source_text",
                                         "carrier_user_id"}),
        "plan": frozenset({"id", "order_id", "version", "route_json", "distance_km",
                           "eta_h", "carrier_org_id", "created_at"}),
        "bid": frozenset({"id", "order_id", "carrier_org_id", "decision",
                          "decided_at", "created_at"}),
    },
}

_KIND_BY_MODEL = {
    ShipperOrder: "order",
    OrderPlan: "plan",
    CarrierBid: "bid",
}


def _columns(model) -> set:
    try:
        return {c.key for c in sa_inspect(model).columns}
    except Exception:  # noqa: BLE001 非 ORM 对象
        return set()


class _View:
    """受限代理：只暴露白名单字段，其余访问一律抛 IsolationError。"""

    __slots__ = ("_obj", "_role", "_kind", "_allowed")

    def __init__(self, obj, role: str, kind: str):
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_role", role)
        object.__setattr__(self, "_kind", kind)
        allowed = (ROLE_FIELDS.get(role) or {}).get(kind)
        if allowed is None:
            raise IsolationError(f"未知角色 {role!r} 或视图类型 {kind!r}")
        object.__setattr__(self, "_allowed", allowed)

    def __getattr__(self, name: str) -> Any:
        if name in self._allowed:
            return getattr(object.__getattribute__(self, "_obj"), name)
        internal = {"_obj", "_role", "_kind", "_allowed"}
        if name in internal or name.startswith("__"):
            raise AttributeError(name)
        obj = object.__getattribute__(self, "_obj")
        if name in _columns(type(obj)):
            raise IsolationError(
                f"{object.__getattribute__(self, '_role')} 无权读取 "
                f"{type(obj).__name__}.{name}")
        raise AttributeError(f"{type(obj).__name__} 没有字段 {name}")

    # ---- 只读语义：代理对象不可写，写操作必须回到真实 ORM 实例 ----
    def __setattr__(self, name: str, value: Any) -> None:
        raise IsolationError("视图是只读的；写操作请使用真实 ORM 实例")

    def as_dict(self) -> Dict[str, Any]:
        """导出可见字段，供 Agent 拼卡片/回复。"""
        obj = object.__getattribute__(self, "_obj")
        return {k: getattr(obj, k) for k in self._allowed if hasattr(obj, k)}

    def unwrap(self):
        """仅限本层内部与测试使用：拿到真实对象。业务代码不得调用。"""
        return object.__getattribute__(self, "_obj")


def wrap(obj, role: str):
    """把 ORM 对象包成角色视图；None 原样返回。"""
    if obj is None:
        return None
    if isinstance(obj, _View):
        return obj
    if isinstance(obj, dict):
        allowed = (ROLE_FIELDS.get(role) or {}).get("order") or frozenset()
        return {k: v for k, v in obj.items() if k in allowed}
    kind = _KIND_BY_MODEL.get(type(obj))
    if kind is None:
        return obj  # 非受控模型（如 ChatSession）不做投影
    return _View(obj, role, kind)


def load_order(db: Session, order_id: Optional[int], role: str):
    """按角色加载订单视图。Agent 读订单一律走这里，不再直接 db.get。"""
    if not order_id:
        return None
    return wrap(db.get(ShipperOrder, int(order_id)), role)


def load_plan(db: Session, plan_id: Optional[int], role: str):
    if not plan_id:
        return None
    return wrap(db.get(OrderPlan, int(plan_id)), role)


def visible_fields(role: str, kind: str = "order") -> FrozenSet[str]:
    """给测试与静态检查用的白名单。"""
    return (ROLE_FIELDS.get(role) or {}).get(kind) or frozenset()


__all__ = ["IsolationError", "ROLE_FIELDS", "wrap", "load_order", "load_plan",
           "visible_fields"]
