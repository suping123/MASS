-- ============================================================================
-- V6：真多 Agent 改造 —— 招标 / 投标 / 裁决
--
-- 为什么要有 tender 表（而不是给 shipper_order 加一个 bidding 状态）：
--   招标只是"这一单正在收标"这件事本身的状态，不是订单状态。
--   订单在招标期间仍然是 published（可被接单）。把两者混在一起会让
--   订单状态机多出一个只为中间流程存在的态，现有 3 处
--   published -> candidate -> claimed 的断言全部要改。
--   分开之后：订单状态机一行不动，招标进度由 tender.status 表达。
--
-- 为什么 carrier_bid 要区分 quoted_price 与 quote_amount：
--   quoted_price 是服务端按挂牌价回填的"你能拿多少"，承运商从来没报过价；
--   quote_amount 才是承运商自己出的价。两个字段并存，
--   改造前后同一张表的数据都能自解释，历史订单不会变成脏数据。
--
-- 幂等：MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，
--       加列/改枚举一律先查 information_schema 再 PREPARE 执行。
--
--   mysql -uroot -p --default-character-set=utf8mb4 < sql/platform_v6.sql
-- ============================================================================
USE sca_platform;

-- ---------------------------------------------------------------- 招标单
CREATE TABLE IF NOT EXISTS tender (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NOT NULL,
  publish_id    BIGINT UNSIGNED NOT NULL,
  plan_id       BIGINT UNSIGNED DEFAULT NULL,
  budget        DECIMAL(12,2) NOT NULL COMMENT '本次招标上限=publish_price',
  round_no      TINYINT NOT NULL DEFAULT 1 COMMENT '协商轮次，1=首轮收标',
  deadline_at   DATETIME NOT NULL COMMENT '收标截止时间',
  status        VARCHAR(16) NOT NULL DEFAULT 'open'
                COMMENT 'open/negotiating/awarded/failed/cancelled',
  winner_bid_id BIGINT UNSIGNED DEFAULT NULL,
  arbitration   JSON DEFAULT NULL COMMENT '裁决证据：每家分数与淘汰原因',
  created_by    VARCHAR(48) NOT NULL COMMENT 'dispatch:agent 或 dispatch',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at     DATETIME DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_tender_status (status, deadline_at),
  KEY idx_tender_order (order_id, round_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='招标单：收标-裁决-协商';

-- ---------------------------------------------------------------- 报价字段
-- quote_amount：承运商自己出的价（与 quoted_price 区分，见文件头说明）
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'quote_amount');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_bid ADD COLUMN quote_amount DECIMAL(12,2) DEFAULT NULL COMMENT ''承运商自报价（区别于服务端回填的 quoted_price）'' AFTER quoted_price',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- quote_eta_h：承运商承诺时效（小时）
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'quote_eta_h');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_bid ADD COLUMN quote_eta_h DECIMAL(8,2) DEFAULT NULL COMMENT ''承运商承诺时效（小时）'' AFTER quote_amount',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- bid_note：报价说明。金额由公式算，这里只放解释（模型可生成），不参与计算
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'bid_note');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_bid ADD COLUMN bid_note VARCHAR(200) DEFAULT NULL COMMENT ''报价说明，不参与金额计算'' AFTER quote_eta_h',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- bid_round：当前报价轮次
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'bid_round');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_bid ADD COLUMN bid_round TINYINT NOT NULL DEFAULT 1 COMMENT ''当前报价轮次'' AFTER bid_note',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- bid_history：每轮报价与还价轨迹（保留谈判过程，事后可回放谁让了多少）
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'bid_history');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_bid ADD COLUMN bid_history JSON DEFAULT NULL COMMENT ''每轮报价/还价轨迹'' AFTER bid_round',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- decision 枚举扩两个值：bid（已投标）/ expired（超时未响应）
-- 用 COLUMN_TYPE 判断，可重复执行
SET @t := (SELECT COLUMN_TYPE FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_bid'
    AND COLUMN_NAME = 'decision');
SET @ddl := IF(@t LIKE '%''bid''%', 'DO 0',
  'ALTER TABLE carrier_bid MODIFY COLUMN decision ENUM(''candidate'',''bid'',''accepted'',''rejected'',''withdrawn'',''expired'') NOT NULL DEFAULT ''candidate'' COMMENT ''candidate=历史试算 bid=已投标 accepted=中标接单 rejected=被拒 withdrawn=弃标 expired=超时''');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------- 审计分域
-- 哪个 agent 做的决定。人工操作留空，agent 写 carrier:12 / dispatch / exception / shipper:5
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sys_audit_log'
    AND COLUMN_NAME = 'agent_key');
SET @ddl := IF(@c = 0,
  'ALTER TABLE sys_audit_log ADD COLUMN agent_key VARCHAR(48) DEFAULT NULL COMMENT ''执行该操作的 agent；人工操作留空'' AFTER actor_role',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------- 裁决权重
-- 权重进库而不是写死在代码里：调权重是业务决定，要留痕（put_config 会写审计）
INSERT INTO system_config (cfg_key, cfg_value, category, note) VALUES
  ('tender.weights', '{"price":0.45,"eta":0.20,"rating":0.20,"deadhead":0.15}',
   'tender', '招标裁决四因子权重，合计必须为 1')
ON DUPLICATE KEY UPDATE category = VALUES(category), note = VALUES(note);

INSERT INTO system_config (cfg_key, cfg_value, category, note) VALUES
  ('tender.max_rounds', '3', 'tender', '协商轮次上限，超过即流标交人')
ON DUPLICATE KEY UPDATE category = VALUES(category), note = VALUES(note);

-- ---------------------------------------------------------------- 承运商资质（S2 补充）
-- 招标的硬过滤要看温控/危品资质，但 carrier_profile 里原本没有这两列，
-- 过滤条件就只能恒假（冷链单永远流标）。补上，且**默认 NULL 表示未登记**：
-- 未登记不等于没有资质，处理方式是"允许投标但标注待核验"，
-- 不替承运商假定它没有资质，也不假装它有。
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_profile'
    AND COLUMN_NAME = 'temp_ctrl');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_profile ADD COLUMN temp_ctrl TINYINT(1) DEFAULT NULL COMMENT ''温控资质；NULL=未登记''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'carrier_profile'
    AND COLUMN_NAME = 'hazmat');
SET @ddl := IF(@c = 0,
  'ALTER TABLE carrier_profile ADD COLUMN hazmat TINYINT(1) DEFAULT NULL COMMENT ''危品资质；NULL=未登记''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------- 协商记录（S3）
-- 谈判轨迹单独一列，不塞进 arbitration：
-- arbitration 是"收标那一刻各家的分数与淘汰原因"，会在每次裁决时被重写；
-- 谈判是过程，跨多轮且要和裁决证据一起看，混在一起会互相覆盖。
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tender'
    AND COLUMN_NAME = 'negotiation');
SET @ddl := IF(@c = 0,
  'ALTER TABLE tender ADD COLUMN negotiation JSON DEFAULT NULL COMMENT ''协商轨迹：每轮谁出了什么价''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 协商超时：超过这个时间没有回音，判无交集交人。
-- 方案里写的是 60s，实施时放宽到 600s：承运商账号可能由人操作，
-- 60 秒内不回复很正常，超时判死会把正常协商误杀。agent 自循环通常在秒级回。
INSERT INTO system_config (cfg_key, cfg_value, category, note) VALUES
  ('tender.counter_timeout_s', '600', 'tender', '协商单轮超时（秒），超时判无交集交人')
ON DUPLICATE KEY UPDATE note = VALUES(note);
