-- ============================================================================
-- V7：四进程常驻 —— 消息租约 / 心跳
--
-- 为什么需要租约（而不是"取走就算处理"）：
--   v5 的 claim() 把消息状态一改就返回，进程如果在处理中途被杀，
--   这条消息既不会被重投，也没有人知道它没做完——队列看起来是空的，
--   业务其实是断的。S4 起四个 agent 是独立进程，随时可能被单独杀掉，
--   所以必须能回答"这条消息现在在谁手上、租约到没到"。
--
--   status 的取值从 pending/consumed/ack/failed 扩成
--   pending -> working -> ack/failed，working 带 lease_owner + lease_until。
--   consumed 保留：v5 的同步 claim() 仍在用，历史数据也还认。
--
-- 为什么要 agent_actor.last_seen_at：
--   HTTP 接口投完消息要等回复。没有心跳就没法区分"agent 正在忙"和
--   "agent 根本没启动"，前者该等，后者该立刻告诉用户去跑 start_agents.bat。
--
-- 幂等：MySQL 8 不支持 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，
--       一律先查 information_schema 再 PREPARE 执行。
--
--   mysql -uroot -p --default-character-set=utf8mb4 < sql/platform_v7.sql
-- ============================================================================
USE sca_platform;

-- ---------------------------------------------------------------- 消息租约
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
    AND COLUMN_NAME = 'lease_owner');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_message ADD COLUMN lease_owner VARCHAR(64) DEFAULT NULL COMMENT ''租约持有者：{agent_key}@{host}:{pid}#{nonce}，用于 fencing'' AFTER status',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
    AND COLUMN_NAME = 'lease_until');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_message ADD COLUMN lease_until DATETIME DEFAULT NULL COMMENT ''租约到期；到期仍为 working 视为进程已死，可被重领'' AFTER lease_owner',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- tries：被领次数。超过上限置 failed，避免一条毒消息被无限重投
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
    AND COLUMN_NAME = 'tries');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_message ADD COLUMN tries INT NOT NULL DEFAULT 0 COMMENT ''被领取次数，超上限置 failed'' AFTER lease_until',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 收件箱索引：领取语句是 (to_agent, status, id) 上的等值+范围
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_message'
    AND INDEX_NAME = 'idx_msg_inbox');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_message ADD INDEX idx_msg_inbox (to_agent, status, id)',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------- Agent 心跳
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_actor'
    AND COLUMN_NAME = 'last_seen_at');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_actor ADD COLUMN last_seen_at DATETIME DEFAULT NULL COMMENT ''最后一次心跳；HTTP 侧据此判断 agent 是否在跑''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_actor'
    AND COLUMN_NAME = 'pid');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_actor ADD COLUMN pid INT DEFAULT NULL COMMENT ''常驻进程 PID，便于排查"谁在消费消息"''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_actor'
    AND COLUMN_NAME = 'host');
SET @ddl := IF(@c = 0,
  'ALTER TABLE agent_actor ADD COLUMN host VARCHAR(64) DEFAULT NULL COMMENT ''进程所在主机名''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
