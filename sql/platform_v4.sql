-- ============================================================================
-- V4：调度助手（dispatch_ops）落库
--
-- 背景：chat_session.scene / state 原来是 ENUM，新增一个场景就必须 ALTER ENUM；
-- 调度台的常驻助手是第五个场景，且 state 用的是 idle/confirming 这套更通用的值。
-- 改成 VARCHAR 后，新增场景只改代码，不再动表结构。
--
-- 幂等：MySQL 8 没有 ADD COLUMN IF NOT EXISTS 那套写法，MODIFY 本身可重复执行，
-- 但为了不每次启动都重建表，这里先用 information_schema 判断类型。
-- ============================================================================
USE sca_platform;

SET @need := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_session'
    AND COLUMN_NAME = 'scene' AND DATA_TYPE = 'enum'
);
SET @ddl := IF(@need > 0,
  'ALTER TABLE chat_session MODIFY scene VARCHAR(24) NOT NULL COMMENT ''场景：shipper_order/carrier_query/carrier_exception/dispatch_ops''',
  'DO 0');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need2 := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_session'
    AND COLUMN_NAME = 'state' AND DATA_TYPE = 'enum'
);
SET @ddl2 := IF(@need2 > 0,
  'ALTER TABLE chat_session MODIFY state VARCHAR(16) NOT NULL DEFAULT ''collecting''',
  'DO 0');
PREPARE stmt2 FROM @ddl2; EXECUTE stmt2; DEALLOCATE PREPARE stmt2;

-- 调度助手按会话反查历史消息，补一个索引（已存在则跳过）
SET @idx := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_message'
    AND INDEX_NAME = 'idx_cm_session'
);
SET @ddl3 := IF(@idx = 0,
  'ALTER TABLE chat_message ADD INDEX idx_cm_session (session_id, id)',
  'DO 0');
PREPARE stmt3 FROM @ddl3; EXECUTE stmt3; DEALLOCATE PREPARE stmt3;
