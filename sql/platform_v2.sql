-- =====================================================================
-- 三平台 v2 迁移：系统日志 / 服务费规则 / 系统配置 / 超管权限点
--
-- 幂等：全部 IF NOT EXISTS + INSERT ... ON DUPLICATE KEY，可重复执行。
-- 执行：mysql -uroot -p --default-character-set=utf8mb4 < sql/platform_v2.sql
-- =====================================================================

SET NAMES utf8mb4;
USE sca_platform;

-- ---------------------------------------------------------------------
-- 一、系统日志（取消、驳回、回退、配置变更的唯一审计入口）
-- ---------------------------------------------------------------------
-- 为什么单独建表而不是塞进 order_event：
--   order_event 是"订单发生了什么"，面向业务回放；
--   sys_audit_log 是"谁做了什么操作"，面向追责与合规。
--   两者的查询维度、保留策略和访问权限都不同（调度员可读、超管可管）。
CREATE TABLE IF NOT EXISTS sys_audit_log (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  actor_user_id BIGINT UNSIGNED NOT NULL,
  actor_account VARCHAR(64)     DEFAULT NULL COMMENT '冗余存一份，账号改名后仍可追溯',
  actor_role   VARCHAR(16)      NOT NULL,
  action       VARCHAR(48)      NOT NULL COMMENT 'order_cancelled / fee_rule_changed / ...',
  target_type  VARCHAR(24)      DEFAULT NULL COMMENT 'order / carrier / shipper / config / knowledge',
  target_id    VARCHAR(64)      DEFAULT NULL,
  reason       VARCHAR(500)     DEFAULT NULL COMMENT '取消原因 / 变更原因（必填项）',
  detail       JSON             DEFAULT NULL,
  ip           VARCHAR(64)      DEFAULT NULL,
  created_at   DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_audit_time (created_at),
  KEY idx_audit_actor (actor_user_id),
  KEY idx_audit_action (action, created_at),
  KEY idx_audit_target (target_type, target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统操作日志：谁在什么时候做了什么，为什么';

-- ---------------------------------------------------------------------
-- 二、平台服务费规则（超管改，调度员只读）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fee_rule (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name           VARCHAR(64)     NOT NULL,
  shipper_rate   DECIMAL(6,4)    NOT NULL COMMENT '货主侧费率，0.0200 = 2%',
  carrier_rate   DECIMAL(6,4)    NOT NULL COMMENT '承运商侧费率',
  status         ENUM('active','archived') NOT NULL DEFAULT 'active',
  effective_from DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note           VARCHAR(200)    DEFAULT NULL,
  created_by     BIGINT UNSIGNED DEFAULT NULL,
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_fee_status (status, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='服务费规则；同一时刻只允许一条 active（应用层保证）';

-- ---------------------------------------------------------------------
-- 三、系统配置（模型降级链 / 其它非敏感开关）
-- ---------------------------------------------------------------------
-- 敏感凭据（模型 Key、高德 Key、钉钉）不进这张表，仍走 data/credentials.json
-- 与 sca.credentials，页面只读写掩码视图。
CREATE TABLE IF NOT EXISTS system_config (
  cfg_key    VARCHAR(64)     NOT NULL,
  cfg_value  JSON            NOT NULL,
  category   VARCHAR(32)     NOT NULL COMMENT 'model / mcp / ui / fee',
  note       VARCHAR(200)    DEFAULT NULL,
  updated_by BIGINT UNSIGNED DEFAULT NULL,
  updated_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (cfg_key),
  KEY idx_cfg_cat (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置（非敏感）';

-- ---------------------------------------------------------------------
-- 四、权限点
-- ---------------------------------------------------------------------
INSERT INTO sys_permission (code, name, module) VALUES
  ('order:cancel',          '取消订单',             'order'),
  ('dispatch:audit_read',   '查看系统日志',         'dispatch'),
  ('dispatch:fee_rule_view','查看服务费规则',       'dispatch'),
  ('dispatch:knowledge',    '知识库管理',           'dispatch'),
  ('dispatch:history',      '历史调度与回退',       'dispatch'),
  ('dispatch:org_view',     '查看货主/承运商信息',  'dispatch'),
  ('admin:audit_manage',    '操作系统日志',         'admin'),
  ('admin:fee_rule',        '服务费规则管理',       'admin'),
  ('admin:model_config',    '模型降级工厂管理',     'admin'),
  ('admin:mcp_key',         'MCP 工具 Key 管理',    'admin'),
  ('admin:org_manage',      '货主/承运商信息管理',  'admin')
ON DUPLICATE KEY UPDATE name = VALUES(name), module = VALUES(module);

-- 角色绑定：admin 拿全部；其余按职责最小授权
INSERT IGNORE INTO sys_role_permission (role_id, perm_id)
SELECT r.id, p.id FROM sys_role r JOIN sys_permission p
WHERE (r.code = 'shipper' AND p.code IN ('order:create','order:read_own','order:cancel'))
   OR (r.code = 'dispatcher' AND p.code IN (
        'dispatch:read_all','dispatch:plan','dispatch:publish','dispatch:exception',
        'dispatch:audit_read','dispatch:fee_rule_view','dispatch:knowledge',
        'dispatch:history','dispatch:org_view','order:cancel'))
   OR (r.code = 'carrier' AND p.code IN (
        'carrier:list','carrier:accept','carrier:report','carrier:read_own','order:cancel'))
   OR (r.code = 'admin' AND p.code IN (
        'admin:user','admin:rbac','admin:audit_manage','admin:fee_rule',
        'admin:model_config','admin:mcp_key','admin:org_manage',
        'dispatch:read_all','dispatch:plan','dispatch:publish','dispatch:exception',
        'dispatch:audit_read','dispatch:fee_rule_view','dispatch:knowledge',
        'dispatch:history','dispatch:org_view','order:cancel',
        'order:create','order:read_own',
        'carrier:list','carrier:accept','carrier:report','carrier:read_own'));

-- ---------------------------------------------------------------------
-- 五、初始数据
-- ---------------------------------------------------------------------
INSERT INTO fee_rule (name, shipper_rate, carrier_rate, status, note)
SELECT '默认规则（公司统一规定）', 0.0200, 0.0200, 'active', '两侧各 2%，按订单金额比例'
WHERE NOT EXISTS (SELECT 1 FROM fee_rule WHERE status = 'active');

INSERT INTO system_config (cfg_key, cfg_value, category, note) VALUES
  ('model.chain',
   JSON_OBJECT(
     'intent', JSON_ARRAY('tongyi-intent-detect-v3', 'rule'),
     'route',  JSON_ARRAY('qwen-plus', 'rule'),
     'note',   '数组顺序即降级顺序；末位 rule 表示规则引擎兜底，永不失败'),
   'model', '模型降级链（首选 -> 降级 -> 规则兜底）'),
  ('model.enabled',
   JSON_OBJECT('intent', TRUE, 'route', FALSE,
               'note', 'route=false 时选路走确定性算法，不调模型'),
   'model', '各能力是否启用模型'),
  ('mcp.tools',
   JSON_OBJECT(
     'amap',    JSON_OBJECT('label', '高德地图 Web 服务', 'required', TRUE),
     'model',   JSON_OBJECT('label', '大模型（DashScope）', 'required', TRUE),
     'dingtalk',JSON_OBJECT('label', '钉钉通知（已停用）', 'required', FALSE)),
   'mcp', '需要的工具凭据清单（值存在 data/credentials.json，此处只放元信息）')
ON DUPLICATE KEY UPDATE note = VALUES(note);
