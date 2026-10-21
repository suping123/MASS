-- =====================================================================
-- 三平台 v3：承运商空驶成本
--
-- 业务口径：承运商当前在 A，订单起点是 B，它要先把空车从 A 开到 B。
-- 这段空驶没有收入，但油费/过路费/人工是真的，必须从收益里扣。
-- 不扣会导致：离起点很远的承运商看到"收益很高"而接单，实际跑完亏钱。
--
-- 为什么把三项都存下来：事后对账时能回答"当时按哪个位置、多少公里、
-- 多少钱算的"，只留一个 profit 数字，争议起来没法复盘。
--
-- ⚠️ MySQL 8 **不支持** `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
--    （那是 MariaDB 的扩展）。这里用 information_schema 判断后动态执行，
--    保证脚本可以重复跑。
--
--   mysql -uroot -p --default-character-set=utf8mb4 < sql/platform_v3.sql
-- =====================================================================

SET NAMES utf8mb4;
USE sca_platform;

-- ---- deadhead_km：空驶里程（承运商当前位置 -> 订单起运地）----
SET @has := (SELECT COUNT(*) FROM information_schema.columns
             WHERE table_schema = DATABASE() AND table_name = 'carrier_bid'
               AND column_name = 'deadhead_km');
SET @ddl := IF(@has = 0,
  'ALTER TABLE carrier_bid ADD COLUMN deadhead_km DECIMAL(10,2) DEFAULT NULL COMMENT ''空驶里程：当前位置到起运地''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---- deadhead_cost：空驶成本 = 空驶里程 × 每公里单价 ----
SET @has := (SELECT COUNT(*) FROM information_schema.columns
             WHERE table_schema = DATABASE() AND table_name = 'carrier_bid'
               AND column_name = 'deadhead_cost');
SET @ddl := IF(@has = 0,
  'ALTER TABLE carrier_bid ADD COLUMN deadhead_cost DECIMAL(12,2) DEFAULT NULL COMMENT ''空驶成本''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---- deadhead_from：计算时用的位置快照（对账用）----
SET @has := (SELECT COUNT(*) FROM information_schema.columns
             WHERE table_schema = DATABASE() AND table_name = 'carrier_bid'
               AND column_name = 'deadhead_from');
SET @ddl := IF(@has = 0,
  'ALTER TABLE carrier_bid ADD COLUMN deadhead_from VARCHAR(255) DEFAULT NULL COMMENT ''计算空驶时的位置快照''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---- linehaul_cost：干线成本 = 订单里程 × 每公里单价 ----
SET @has := (SELECT COUNT(*) FROM information_schema.columns
             WHERE table_schema = DATABASE() AND table_name = 'carrier_bid'
               AND column_name = 'linehaul_cost');
SET @ddl := IF(@has = 0,
  'ALTER TABLE carrier_bid ADD COLUMN linehaul_cost DECIMAL(12,2) DEFAULT NULL COMMENT ''干线运输成本''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 收益公式口径写进配置，前端与文档引用同一句话
INSERT INTO system_config (cfg_key, cfg_value, category, note) VALUES
  ('biz.profit_formula',
   JSON_OBJECT(
     'formula', '收益 = 运费 − 承运商侧服务费 − 干线成本 − 空驶成本',
     'linehaul', '干线成本 = 订单里程 × 每公里单价',
     'deadhead', '空驶成本 = (承运商当前位置 → 订单起运地) 里程 × 每公里单价',
     'unknown_position', '位置无法识别时空驶成本按 0 计，并标注「空驶未计」'),
   'biz', '承运商收益计算口径（页面与文档引用此定义）')
ON DUPLICATE KEY UPDATE cfg_value = VALUES(cfg_value), note = VALUES(note);
