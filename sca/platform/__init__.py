"""三平台调度系统（货主 / 调度 / 承运商）。

与既有 `sca.agents` 单图演示系统的关系：
  * `sca.agents` 仍是调度侧的算法内核（路线/运力/仲裁/处置），本包复用它；
  * 本包负责平台化：用户与 RBAC、订单主数据与事件流、三端接口与对话式 Agent。

设计约束（详见 sql/schema.sql 头部注释）：
  1) 订单状态只有一份真相（shipper_order.status），变更先写 order_event；
  2) 金额一律 Decimal；
  3) agent 的私有数据不跨平台暴露（预算不给承运商、成本不给货主）。
"""

__all__ = ["config", "db", "models", "security"]
