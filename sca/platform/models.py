"""ORM 映射（对应 sql/schema.sql 的 15 张表）。

不执行 DDL：表由 SQL 脚本建立，此处只做读写映射，避免两份定义漂移。
字段名与列名保持一一对应，不做隐式重命名，便于用原生 SQL 排查问题。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (JSON, Boolean, DateTime, Integer, Numeric, SmallInteger,
                        String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 身份与权限
class SysOrg(Base):
    __tablename__ = "sys_org"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    org_type: Mapped[str] = mapped_column(String(16))     # shipper / carrier / platform
    contact_name: Mapped[Optional[str]] = mapped_column(String(64))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[int] = mapped_column(SmallInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SysUser(Base):
    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))          # shipper / dispatcher / carrier / admin
    org_id: Mapped[Optional[int]] = mapped_column(Integer)
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[int] = mapped_column(SmallInteger, default=1)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class SysRole(Base):
    __tablename__ = "sys_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))


class SysPermission(Base):
    __tablename__ = "sys_permission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    module: Mapped[str] = mapped_column(String(32))


class SysRolePermission(Base):
    __tablename__ = "sys_role_permission"

    role_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    perm_id: Mapped[int] = mapped_column(Integer, primary_key=True)


# ---------------------------------------------------------------- 订单主链
class ShipperOrder(Base):
    __tablename__ = "shipper_order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32))
    shipper_user_id: Mapped[int] = mapped_column(Integer)
    shipper_org_id: Mapped[Optional[int]] = mapped_column(Integer)
    source_text: Mapped[Optional[str]] = mapped_column(Text)
    origin: Mapped[Optional[str]] = mapped_column(String(64))
    dest: Mapped[Optional[str]] = mapped_column(String(64))
    cargo_type: Mapped[Optional[str]] = mapped_column(String(32))
    weight_t: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    temp_ctrl: Mapped[bool] = mapped_column(Boolean, default=False)
    hazmat: Mapped[bool] = mapped_column(Boolean, default=False)
    required_by: Mapped[Optional[str]] = mapped_column(String(64))
    required_by_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    budget_max: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    contact_name: Mapped[Optional[str]] = mapped_column(String(64))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    active_plan_id: Mapped[Optional[int]] = mapped_column(Integer)
    carrier_org_id: Mapped[Optional[int]] = mapped_column(Integer)
    carrier_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class OrderPlan(Base):
    __tablename__ = "order_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    route_json: Mapped[Optional[dict]] = mapped_column(JSON)
    distance_km: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    eta_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    cost_est: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    arbitration: Mapped[Optional[dict]] = mapped_column(JSON)
    carrier_org_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class OrderPublish(Base):
    __tablename__ = "order_publish"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    plan_id: Mapped[int] = mapped_column(Integer)
    gross_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    shipper_fee_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.02"))
    shipper_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    carrier_fee_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.02"))
    publish_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), default="open")
    published_by: Mapped[Optional[int]] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ---------------------------------------------------------------- 承运商
class CarrierProfile(Base):
    __tablename__ = "carrier_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(Integer)
    carrier_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    cost_per_km: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("2.60"))
    capacity_t: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("30.00"))
    current_position: Mapped[Optional[str]] = mapped_column(String(255))
    position_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rating: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("5.00"))
    completed_orders: Mapped[int] = mapped_column(Integer, default=0)
    # 资质：NULL 表示**未登记**，不是"没有"。招标硬过滤遇到 NULL 时不淘汰，
    # 而是标注「待核验」——替承运商假定"没有资质"和假装"有资质"一样错。
    temp_ctrl: Mapped[Optional[bool]] = mapped_column(Boolean)
    hazmat: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class Tender(Base):
    """招标单。招标的"进行中"是这一行，不是订单状态。

    订单状态机只表达"这单在业务上走到哪"，招标只表达"这单正在收标"。
    两者分开，``published -> candidate -> claimed`` 这条既有路径不受影响。
    """
    __tablename__ = "tender"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    publish_id: Mapped[int] = mapped_column(Integer)
    plan_id: Mapped[Optional[int]] = mapped_column(Integer)
    budget: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="open")
    winner_bid_id: Mapped[Optional[int]] = mapped_column(Integer)
    # 裁决证据：每家报价的分数、淘汰原因、权重版本。没有它就只能说"选了个最好的"，
    # 说不出为什么不是另一家——这正是改造前 order_plan.arbitration 一直空着的地方。
    arbitration: Mapped[Optional[dict]] = mapped_column(JSON)
    # 协商轨迹：每轮谁出了什么价、为什么谈崩。与 arbitration 分开存——
    # 后者是"收标那一刻"的切片，每次裁决重写；前者是过程，跨多轮累积。
    negotiation: Mapped[Optional[dict]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(48), default="dispatch")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class CarrierBid(Base):
    __tablename__ = "carrier_bid"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    publish_id: Mapped[int] = mapped_column(Integer)
    carrier_org_id: Mapped[int] = mapped_column(Integer)
    carrier_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(16), default="candidate")
    # quoted_price 是服务端按挂牌价回填的"你能拿多少"；quote_amount 才是承运商自报价。
    # 两个字段并存，历史数据仍能自解释。
    quoted_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    quote_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    quote_eta_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    bid_note: Mapped[Optional[str]] = mapped_column(String(200))
    bid_round: Mapped[int] = mapped_column(Integer, default=1)
    bid_history: Mapped[Optional[list]] = mapped_column(JSON)
    est_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    est_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    est_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    # 成本拆两段留痕：干线（有收入那段）与空驶（纯支出那段）。
    # 只存一个 est_cost 的话，事后没法回答"空车那段算没算、算了多少"。
    linehaul_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    deadhead_km: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    deadhead_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    deadhead_from: Mapped[Optional[str]] = mapped_column(String(255))
    position_text: Mapped[Optional[str]] = mapped_column(String(255))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------------------------------------------------------------- 事件与异常
class OrderEvent(Base):
    __tablename__ = "order_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    actor_role: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ExceptionReport(Base):
    __tablename__ = "exception_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    reporter_user_id: Mapped[int] = mapped_column(Integer)
    reporter_org_id: Mapped[Optional[int]] = mapped_column(Integer)
    position_text: Mapped[Optional[str]] = mapped_column(String(255))
    exception_code: Mapped[Optional[str]] = mapped_column(String(32))
    exception_name: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    level: Mapped[Optional[str]] = mapped_column(String(4))
    # 处置方式与改期幅度：自由文本 resolution 无法聚合统计，这两列补上
    resolution_action: Mapped[Optional[str]] = mapped_column(String(16))
    deadline_shift_h: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolution: Mapped[Optional[str]] = mapped_column(Text)
    resolved_by_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Settlement(Base):
    __tablename__ = "settlement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    shipper_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    carrier_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    carrier_income: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ---------------------------------------------------------------- 对话
class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    scene: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16), default="collecting")
    slots: Mapped[Optional[dict]] = mapped_column(JSON)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    order_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(8))     # user / agent / system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------------------------------------------------------------- 运维与治理
class SysAuditLog(Base):
    """系统操作日志。

    与 order_event 的分工：order_event 记录"订单发生了什么"（业务回放），
    这里记录"谁做了什么操作"（追责与合规）。取消、驳回、回退、配置变更都写这里。
    """
    __tablename__ = "sys_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int] = mapped_column(Integer)
    actor_account: Mapped[Optional[str]] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(16))
    # 是哪个 agent 做的决定。人工操作留空；agent 写 carrier:12 / dispatch / exception。
    # 没有这一列，"谁越权了"和"谁拍的板"只能靠猜日志顺序。
    agent_key: Mapped[Optional[str]] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[Optional[str]] = mapped_column(String(24))
    target_id: Mapped[Optional[str]] = mapped_column(String(64))
    reason: Mapped[Optional[str]] = mapped_column(String(500))
    detail: Mapped[Optional[dict]] = mapped_column(JSON)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FeeRule(Base):
    __tablename__ = "fee_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    shipper_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    carrier_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    status: Mapped[str] = mapped_column(String(16), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    note: Mapped[Optional[str]] = mapped_column(String(200))
    created_by: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SystemConfig(Base):
    __tablename__ = "system_config"

    cfg_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cfg_value: Mapped[dict] = mapped_column(JSON)
    category: Mapped[str] = mapped_column(String(32))
    note: Mapped[Optional[str]] = mapped_column(String(200))
    updated_by: Mapped[Optional[int]] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


# ---------------------------------------------------------------- 多 Agent（V5）
class AgentActor(Base):
    """Agent 身份与目标函数。

    `utility` 是各 Agent 的偏好权重。同一笔订单，货主看重成本、承运商看重利润、
    调度看重时效与风险——权重不同才会有真实分歧，多 Agent 才有存在的意义。
    若所有 Agent 目标一致，那就该合并成一个 Agent，不该硬拆。
    """
    __tablename__ = "agent_actor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_key: Mapped[str] = mapped_column(String(48))
    kind: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    org_id: Mapped[Optional[int]] = mapped_column(Integer)
    utility: Mapped[Optional[dict]] = mapped_column(JSON)
    enabled: Mapped[int] = mapped_column(SmallInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 心跳：HTTP 侧投完消息要等回复，靠它区分"agent 在忙"与"agent 没启动"。
    # 没有它，两种情况都是 5 秒超时，用户只会看到"调度无响应"这一句废话。
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    pid: Mapped[Optional[int]] = mapped_column(Integer)
    host: Mapped[Optional[str]] = mapped_column(String(64))


class AgentMemory(Base):
    """Agent 私有记忆：按 `agent_key` 隔离，跨域不可见。

    这是"多 Agent"与"一个 Agent 多角色"的实质区别所在——
    承运商记得"这条线我上次成本 2.8/km"，货主记得"我最多接受 8% 上浮"，
    这些都不进共享状态。
    """
    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_key: Mapped[str] = mapped_column(String(48))
    mkey: Mapped[str] = mapped_column(String(64))
    mval: Mapped[Optional[dict]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class AgentMessage(Base):
    """Agent 间消息：多 Agent 的唯一通信通道。

    禁止 Agent 直接调用另一个 Agent 的实现，也禁止读对方记忆；
    跨域取数一律发 `request`，由持有方按白名单返回。
    """
    __tablename__ = "agent_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(48))
    parent_id: Mapped[Optional[int]] = mapped_column(Integer)
    from_agent: Mapped[str] = mapped_column(String(48))
    to_agent: Mapped[str] = mapped_column(String(48))
    msg_type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # 租约：pending -> working -> ack/failed。working 时写 lease_owner/lease_until，
    # 进程被杀后租约到期即可被重领——"取走就算处理完"在常驻进程里是不成立的。
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64))
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    tries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Notification(Base):
    """站内通知。改造前平台没有任何通知落库机制，货主只能靠刷新自己发现变更。"""
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[Optional[int]] = mapped_column(Integer)
    ntype: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[Optional[str]] = mapped_column(Text)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
