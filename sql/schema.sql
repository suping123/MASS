-- =====================================================================
-- 供应链三平台调度系统 · 建表脚本
-- MySQL 8.0+（已在 8.4.5 验证）
--
-- 执行方式（命令行）：
--   "C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe" -uroot -p ^
--       --default-character-set=utf8mb4 < sql/schema.sql
--
-- 设计要点：
--   1) 订单状态只有一份真相（shipper_order.status），所有变更先写
--      order_event（只追加），由事件驱动状态推进，三端只读各自视图。
--   2) carrier_bid 的 (order_id, carrier_org_id) 唯一约束是"重复接单"
--      的幂等闸口，不是普通索引。
--   3) 金额字段一律 DECIMAL，禁止用浮点算钱。
-- =====================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS sca_platform
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE sca_platform;

-- ---------------------------------------------------------------------
-- 一、身份与权限
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sys_org (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name          VARCHAR(128)    NOT NULL COMMENT '企业名称',
  org_type      ENUM('shipper','carrier','platform') NOT NULL COMMENT '货主/承运商/平台方',
  contact_name  VARCHAR(64)     DEFAULT NULL,
  contact_phone VARCHAR(32)     DEFAULT NULL,
  status        TINYINT         NOT NULL DEFAULT 1 COMMENT '1正常 0停用',
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_org_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='组织：货主企业 / 承运商企业 / 平台方';

CREATE TABLE IF NOT EXISTS sys_user (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account       VARCHAR(64)     NOT NULL COMMENT '登录账号（全局唯一，三端共用一张表）',
  password_hash VARCHAR(255)    NOT NULL COMMENT 'pbkdf2_sha256$迭代$盐$哈希',
  display_name  VARCHAR(64)     NOT NULL,
  role          ENUM('shipper','dispatcher','carrier','admin') NOT NULL,
  org_id        BIGINT UNSIGNED DEFAULT NULL,
  phone         VARCHAR(32)     DEFAULT NULL,
  status        TINYINT         NOT NULL DEFAULT 1,
  last_login_at DATETIME        DEFAULT NULL,
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_account (account),
  KEY idx_role (role),
  KEY idx_org (org_id),
  CONSTRAINT fk_user_org FOREIGN KEY (org_id) REFERENCES sys_org (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='统一用户表，role 决定能进哪个端';

CREATE TABLE IF NOT EXISTS sys_role (
  id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code  VARCHAR(32)     NOT NULL,
  name  VARCHAR(64)     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_role_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色字典';

CREATE TABLE IF NOT EXISTS sys_permission (
  id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code   VARCHAR(64)     NOT NULL COMMENT '如 order:create',
  name   VARCHAR(64)     NOT NULL,
  module VARCHAR(32)     NOT NULL COMMENT '归属模块，便于前端按模块渲染菜单',
  PRIMARY KEY (id),
  UNIQUE KEY uk_perm_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限点字典';

CREATE TABLE IF NOT EXISTS sys_role_permission (
  role_id BIGINT UNSIGNED NOT NULL,
  perm_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (role_id, perm_id),
  CONSTRAINT fk_rp_role FOREIGN KEY (role_id) REFERENCES sys_role (id) ON DELETE CASCADE,
  CONSTRAINT fk_rp_perm FOREIGN KEY (perm_id) REFERENCES sys_permission (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色-权限绑定';

-- ---------------------------------------------------------------------
-- 二、订单主链
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shipper_order (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no        VARCHAR(32)     NOT NULL,
  shipper_user_id BIGINT UNSIGNED NOT NULL,
  shipper_org_id  BIGINT UNSIGNED DEFAULT NULL,
  source_text     TEXT            DEFAULT NULL COMMENT '货主原话，保留以便追溯',
  origin          VARCHAR(64)     DEFAULT NULL,
  dest            VARCHAR(64)     DEFAULT NULL,
  cargo_type      VARCHAR(32)     DEFAULT NULL,
  weight_t        DECIMAL(10,2)   DEFAULT NULL,
  temp_ctrl       TINYINT         NOT NULL DEFAULT 0,
  hazmat          TINYINT         NOT NULL DEFAULT 0,
  required_by     VARCHAR(64)     DEFAULT NULL COMMENT '时效要求原文',
  required_by_at  DATETIME        DEFAULT NULL COMMENT '归一化后的最晚到达时间',
  budget_max      DECIMAL(12,2)   DEFAULT NULL COMMENT '货主预算上限（私有，不下发给承运商）',
  contact_name    VARCHAR(64)     DEFAULT NULL,
  contact_phone   VARCHAR(32)     DEFAULT NULL,
  status          VARCHAR(24)     NOT NULL DEFAULT 'draft'
                  COMMENT 'draft/submitted/planned/published/candidate/claimed/in_transit/exception/delivered/settled/cancelled',
  active_plan_id  BIGINT UNSIGNED DEFAULT NULL,
  carrier_org_id  BIGINT UNSIGNED DEFAULT NULL,
  carrier_user_id BIGINT UNSIGNED DEFAULT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_no (order_no),
  KEY idx_shipper (shipper_user_id, created_at),
  KEY idx_status (status, created_at),
  KEY idx_carrier (carrier_org_id, status),
  CONSTRAINT fk_order_shipper FOREIGN KEY (shipper_user_id) REFERENCES sys_user (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单主表：状态唯一事实源';

CREATE TABLE IF NOT EXISTS order_plan (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NOT NULL,
  version       INT             NOT NULL DEFAULT 1,
  route_json    JSON            DEFAULT NULL COMMENT '途经点/里程/降级原因等完整路线',
  distance_km   DECIMAL(10,2)   DEFAULT NULL,
  eta_h         DECIMAL(8,2)    DEFAULT NULL,
  cost_est      DECIMAL(12,2)   DEFAULT NULL,
  arbitration   JSON            DEFAULT NULL COMMENT '裁决证据：各路结论与选定依据',
  carrier_org_id BIGINT UNSIGNED DEFAULT NULL,
  created_by_user_id BIGINT UNSIGNED DEFAULT NULL,
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_plan_version (order_id, version),
  KEY idx_plan_order (order_id),
  CONSTRAINT fk_plan_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调度方案版本（重规划即版本+1）';

CREATE TABLE IF NOT EXISTS order_publish (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id         BIGINT UNSIGNED NOT NULL,
  plan_id          BIGINT UNSIGNED NOT NULL,
  gross_price      DECIMAL(12,2)   NOT NULL COMMENT '货主侧应付总额（含运费，不含货主服务费）',
  shipper_fee_rate DECIMAL(6,4)    NOT NULL DEFAULT 0.0200,
  shipper_fee      DECIMAL(12,2)   NOT NULL DEFAULT 0,
  carrier_fee_rate DECIMAL(6,4)    NOT NULL DEFAULT 0.0200,
  publish_price    DECIMAL(12,2)   NOT NULL COMMENT '承运商看到的运费（已扣货主侧服务费）',
  status           ENUM('open','closed','cancelled') NOT NULL DEFAULT 'open',
  published_by     BIGINT UNSIGNED DEFAULT NULL,
  published_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at        DATETIME        DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_pub_order (order_id),
  KEY idx_pub_status (status, published_at),
  CONSTRAINT fk_pub_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE,
  CONSTRAINT fk_pub_plan FOREIGN KEY (plan_id) REFERENCES order_plan (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='挂牌：调度发布到承运商公共订单池';

-- ---------------------------------------------------------------------
-- 三、承运商
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS carrier_profile (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  org_id             BIGINT UNSIGNED NOT NULL,
  carrier_user_id    BIGINT UNSIGNED DEFAULT NULL,
  cost_per_km        DECIMAL(8,2)    NOT NULL DEFAULT 2.60 COMMENT '承运商每公里成本',
  capacity_t         DECIMAL(10,2)   NOT NULL DEFAULT 30.00,
  current_position   VARCHAR(255)    DEFAULT NULL COMMENT '文本位置：由承运商自行填写',
  position_updated_at DATETIME       DEFAULT NULL,
  rating             DECIMAL(3,2)    NOT NULL DEFAULT 5.00,
  completed_orders   INT             NOT NULL DEFAULT 0,
  created_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_carrier_org (org_id),
  CONSTRAINT fk_cp_org FOREIGN KEY (org_id) REFERENCES sys_org (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='承运商画像：单价、吨位、文本位置';

CREATE TABLE IF NOT EXISTS carrier_bid (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id        BIGINT UNSIGNED NOT NULL,
  publish_id      BIGINT UNSIGNED NOT NULL,
  carrier_org_id  BIGINT UNSIGNED NOT NULL,
  carrier_user_id BIGINT UNSIGNED DEFAULT NULL,
  decision        ENUM('candidate','accepted','rejected','withdrawn') NOT NULL DEFAULT 'candidate',
  quoted_price    DECIMAL(12,2)   DEFAULT NULL COMMENT '承运商可得运费（=publish_price）',
  est_cost        DECIMAL(12,2)   DEFAULT NULL COMMENT '估算成本=里程×单价',
  est_fee         DECIMAL(12,2)   DEFAULT NULL COMMENT '承运商侧平台服务费',
  est_profit      DECIMAL(12,2)   DEFAULT NULL COMMENT '实际收益=运费-成本-服务费',
  position_text   VARCHAR(255)    DEFAULT NULL COMMENT '接单时承运商自报的位置',
  decided_at      DATETIME        DEFAULT NULL,
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_bid_order_carrier (order_id, carrier_org_id),
  KEY idx_bid_carrier (carrier_org_id, decision),
  CONSTRAINT fk_bid_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE,
  CONSTRAINT fk_bid_publish FOREIGN KEY (publish_id) REFERENCES order_publish (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='承运商决策与收益试算；唯一键=重复接单的幂等闸口';

-- ---------------------------------------------------------------------
-- 四、事件流与异常
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS order_event (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NOT NULL,
  seq           INT             NOT NULL COMMENT '同一订单内递增，唯一键保证只追加不重放',
  event_type    VARCHAR(48)     NOT NULL,
  actor_role    VARCHAR(16)     NOT NULL COMMENT 'shipper/dispatcher/carrier/system',
  actor_user_id BIGINT UNSIGNED DEFAULT NULL,
  payload       JSON            DEFAULT NULL,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_event_seq (order_id, seq),
  KEY idx_event_order (order_id, created_at),
  CONSTRAINT fk_event_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单事件流：平台间信息流动的唯一通道，只追加';

CREATE TABLE IF NOT EXISTS exception_report (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id           BIGINT UNSIGNED NOT NULL,
  reporter_user_id   BIGINT UNSIGNED NOT NULL,
  reporter_org_id    BIGINT UNSIGNED DEFAULT NULL,
  position_text      VARCHAR(255)    DEFAULT NULL COMMENT '承运商自报位置（替代 IoT）',
  exception_code     VARCHAR(32)     DEFAULT NULL,
  exception_name     VARCHAR(64)     DEFAULT NULL,
  description        TEXT            DEFAULT NULL,
  level              VARCHAR(4)      DEFAULT NULL COMMENT 'P0/P1/P2/P3，由调度侧判定',
  status             ENUM('open','handling','resolved') NOT NULL DEFAULT 'open',
  resolution         TEXT            DEFAULT NULL COMMENT '调度员处置结果',
  resolved_by_user_id BIGINT UNSIGNED DEFAULT NULL,
  resolved_at        DATETIME        DEFAULT NULL,
  created_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_exc_order (order_id, status),
  KEY idx_exc_status (status, created_at),
  CONSTRAINT fk_exc_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='承运商上报的异常及调度处置结果';

-- ---------------------------------------------------------------------
-- 五、结算
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS settlement (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NOT NULL,
  gross_amount   DECIMAL(12,2)   NOT NULL COMMENT '货主实付总额',
  shipper_fee    DECIMAL(12,2)   NOT NULL DEFAULT 0 COMMENT '货主侧服务费',
  carrier_fee    DECIMAL(12,2)   NOT NULL DEFAULT 0 COMMENT '承运商侧服务费',
  carrier_income DECIMAL(12,2)   NOT NULL COMMENT '承运商实收 = gross - shipper_fee - carrier_fee',
  status         ENUM('pending','settled') NOT NULL DEFAULT 'pending',
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  settled_at     DATETIME        DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_settle_order (order_id),
  CONSTRAINT fk_settle_order FOREIGN KEY (order_id) REFERENCES shipper_order (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='结算：金额守恒的唯一校验点';

-- ---------------------------------------------------------------------
-- 六、对话
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS chat_session (
  id         VARCHAR(64)     NOT NULL COMMENT '会话ID（uuid4 hex）',
  user_id    BIGINT UNSIGNED NOT NULL,
  -- scene / state 用 VARCHAR 而不是 ENUM：每加一个场景（例如调度助手 dispatch_ops）
  -- 都要 ALTER ENUM，代价高且容易漏；场景值由代码常量约束，不在库层枚举。
  scene      VARCHAR(24)     NOT NULL,
  state      VARCHAR(16)     NOT NULL DEFAULT 'collecting',
  slots      JSON            DEFAULT NULL COMMENT '已收集槽位快照',
  rounds     INT             NOT NULL DEFAULT 0 COMMENT '已完成轮次（有界自主的计量口径）',
  order_id   BIGINT UNSIGNED DEFAULT NULL,
  created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cs_user (user_id, scene, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='对话会话：槽位状态机，跨请求保持';

CREATE TABLE IF NOT EXISTS chat_message (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id VARCHAR(64)     NOT NULL,
  user_id    BIGINT UNSIGNED DEFAULT NULL,
  role       ENUM('user','agent','system') NOT NULL,
  content    TEXT            NOT NULL,
  meta       JSON            DEFAULT NULL COMMENT '工具调用留痕、槽位变化等',
  created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cm_session (session_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='对话消息（含 agent 回复与工具留痕）';

-- ---------------------------------------------------------------------
-- 七、字典数据：角色与权限
-- ---------------------------------------------------------------------

INSERT INTO sys_role (code, name) VALUES
  ('shipper',    '货主'),
  ('dispatcher', '调度员'),
  ('carrier',    '承运商'),
  ('admin',      '平台管理员')
ON DUPLICATE KEY UPDATE name = VALUES(name);

INSERT INTO sys_permission (code, name, module) VALUES
  ('order:create',          '发布运输需求',       'shipper'),
  ('order:read_own',        '查看自己的订单',     'shipper'),
  ('dispatch:read_all',     '查看全部订单',       'dispatch'),
  ('dispatch:plan',         '生成调度方案',       'dispatch'),
  ('dispatch:publish',      '发布到承运商订单池', 'dispatch'),
  ('dispatch:exception',    '处置异常上报',       'dispatch'),
  ('carrier:list',          '查看公共订单池',     'carrier'),
  ('carrier:accept',        '接单',               'carrier'),
  ('carrier:report',        '上报异常',           'carrier'),
  ('carrier:read_own',      '查看自己的运单',     'carrier'),
  ('admin:user',            '用户管理',           'admin'),
  ('admin:rbac',            '角色权限管理',       'admin')
ON DUPLICATE KEY UPDATE name = VALUES(name), module = VALUES(module);

-- 角色-权限绑定（幂等：重复执行不报错）
INSERT IGNORE INTO sys_role_permission (role_id, perm_id)
SELECT r.id, p.id FROM sys_role r JOIN sys_permission p
WHERE (r.code = 'shipper'    AND p.code IN ('order:create','order:read_own'))
   OR (r.code = 'dispatcher' AND p.code IN ('dispatch:read_all','dispatch:plan','dispatch:publish','dispatch:exception'))
   OR (r.code = 'carrier'    AND p.code IN ('carrier:list','carrier:accept','carrier:report','carrier:read_own'))
   OR (r.code = 'admin');

-- =====================================================================
-- 建表完成。演示账号请用种子脚本创建（密码哈希需在 Python 侧生成）：
--   ./.venv/Scripts/python.exe -m sca.platform.seed
-- =====================================================================
