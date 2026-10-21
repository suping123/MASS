-- ============================================================================
-- V5：多 Agent 架构（身份 / 私有记忆 / A2A 消息 / 站内通知）
--
-- 设计原则（为什么是这四张表）：
--   1. agent_actor   —— 每个 Agent 是一个有身份的实体，带自己的目标权重（utility）。
--                        没有这张表，"多 Agent"就只是几个函数的命名风格。
--   2. agent_memory  —— 私有记忆，按 agent_key 隔离。跨域不可见是**查询约束**，
--                        不是口头约定：所有读写都必须带自己的 agent_key。
--   3. agent_message —— Agent 之间唯一的通信通道。禁止 Agent 直接调用另一个 Agent
--                        或读它的记忆；跨域取数只能发 request 消息。
--                        这张表同时是"多 Agent 协作"的证据表：一次跨平台任务的
--                        全部往复都可回放。
--   4. notification  —— 站内通知。改造前的平台没有任何通知落库机制，货主收不到
--                        时效变更、改派、异常上报，只能靠刷新页面自己发现。
--
-- 幂等：MySQL 8 没有 ADD COLUMN IF NOT EXISTS，加列先用 information_schema 判断再
--       PREPARE 执行；建表用 IF NOT EXISTS。整个脚本可重复执行。
-- ============================================================================
USE sca_platform;

-- ---------------------------------------------------------------- Agent 身份
CREATE TABLE IF NOT EXISTS agent_actor (
  id          INT PRIMARY KEY AUTO_INCREMENT,
  agent_key   VARCHAR(48) NOT NULL COMMENT 'shipper:5 / dispatch / carrier:12 / exception',
  kind        VARCHAR(16) NOT NULL COMMENT 'shipper/dispatch/carrier/exception',
  user_id     INT NULL,
  org_id      INT NULL,
  utility     JSON NULL COMMENT '目标权重，如 {"cost":0.5,"sla":0.3,"risk":0.2}',
  enabled     TINYINT NOT NULL DEFAULT 1,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_agent_key (agent_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 身份与目标函数';

-- ---------------------------------------------------------------- 私有记忆
CREATE TABLE IF NOT EXISTS agent_memory (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  agent_key   VARCHAR(48) NOT NULL,
  mkey        VARCHAR(64) NOT NULL,
  mval        JSON NULL,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_agent_mkey (agent_key, mkey)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 私有记忆（跨域不可见）';

-- ---------------------------------------------------------------- A2A 消息
CREATE TABLE IF NOT EXISTS agent_message (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  thread_id    VARCHAR(48) NOT NULL COMMENT 'order:61 / exc:12',
  parent_id    BIGINT NULL COMMENT '往复引用：counter 指向被回应的 propose',
  from_agent   VARCHAR(48) NOT NULL,
  to_agent     VARCHAR(48) NOT NULL,
  msg_type     VARCHAR(16) NOT NULL COMMENT 'propose/counter/accept/reject/request/escalate/notify',
  payload      JSON NULL,
  status       VARCHAR(12) NOT NULL DEFAULT 'pending' COMMENT 'pending/consumed/ack/failed',
  error        VARCHAR(255) NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  consumed_at  DATETIME NULL,
  KEY idx_msg_to (to_agent, status, id),
  KEY idx_msg_thread (thread_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 间消息（唯一通信通道）';

-- ---------------------------------------------------------------- 站内通知
CREATE TABLE IF NOT EXISTS notification (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id     INT NOT NULL,
  order_id    INT NULL,
  ntype       VARCHAR(24) NOT NULL COMMENT 'deadline_changed/reassigned/exception/plan',
  title       VARCHAR(128) NOT NULL,
  body        TEXT NULL,
  read_at     DATETIME NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_nt_user (user_id, read_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站内通知（三端铃铛）';

-- ---------------------------------------------------------------- 异常单加两列
-- 用途：事后能按"处置方式"统计（改期 vs 改道 vs 改派），以及算改期幅度。
-- 之前只有自由文本 resolution，没法聚合。
SET @c1 := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'exception_report'
    AND COLUMN_NAME = 'resolution_action');
SET @ddl1 := IF(@c1 = 0,
  'ALTER TABLE exception_report ADD COLUMN resolution_action VARCHAR(16) NULL COMMENT ''resume/replan/extend/deliver/reassign/replan_skipped'' AFTER level',
  'DO 0');
PREPARE s1 FROM @ddl1; EXECUTE s1; DEALLOCATE PREPARE s1;

SET @c2 := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'exception_report'
    AND COLUMN_NAME = 'deadline_shift_h');
SET @ddl2 := IF(@c2 = 0,
  'ALTER TABLE exception_report ADD COLUMN deadline_shift_h DECIMAL(6,2) NULL COMMENT ''本次处置延长的时效（小时）'' AFTER resolution_action',
  'DO 0');
PREPARE s2 FROM @ddl2; EXECUTE s2; DEALLOCATE PREPARE s2;

-- ---------------------------------------------------------------- 改派权限点
-- 换承运商是强制审批项，必须单独授权：不是所有能处置异常的人都能换人。
INSERT INTO sys_permission (code, name, module) VALUES
  ('dispatch:reassign', '改派承运商', 'dispatch')
ON DUPLICATE KEY UPDATE name = VALUES(name), module = VALUES(module);

INSERT IGNORE INTO sys_role_permission (role_id, perm_id)
SELECT r.id, p.id FROM sys_role r JOIN sys_permission p
WHERE r.code IN ('admin', 'dispatcher') AND p.code = 'dispatch:reassign';
