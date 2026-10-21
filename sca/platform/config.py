"""平台侧配置。

沿用既有 `sca.settings` 的约定：一律走环境变量（前缀 SCA_），.env 自动加载，
代码里不出现任何凭据明文。此处只收平台化新增的配置项，不动既有调度内核的开关。
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """最小 .env 解析（不引入依赖）。已存在的环境变量优先，不覆盖。"""
    p = ROOT / ".env"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_dotenv()


def _s(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _b(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---- MySQL ---------------------------------------------------------------
DB_HOST = _s("SCA_MYSQL_HOST", "127.0.0.1")
DB_PORT = _i("SCA_MYSQL_PORT", 3306)
DB_USER = _s("SCA_MYSQL_USER", "root")
DB_PASSWORD = _s("SCA_MYSQL_PASSWORD", "")
DB_NAME = _s("SCA_MYSQL_DB", "sca_platform")

DB_POOL_SIZE = _i("SCA_MYSQL_POOL_SIZE", 8)
DB_POOL_RECYCLE = _i("SCA_MYSQL_POOL_RECYCLE", 1800)  # MySQL 默认 wait_timeout 8h，提前回收


def db_url(user: str = "", password: str = "") -> str:
    """拼装 SQLAlchemy URL。

    `user`/`password` 允许覆盖：三个 agent 用各自独立的数据库账号，
    越权在数据库层就物理不可能（RBAC 只挡 HTTP，挡不住 SQL）。
    """
    u = user or DB_USER
    p = password or DB_PASSWORD
    return (f"mysql+pymysql://{quote_plus(u)}:{quote_plus(p)}"
            f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4")


# ---- Redis（图形验证码 / 会话 / 限流）------------------------------------
REDIS_URL = _s("SCA_REDIS_URL", "redis://127.0.0.1:6379/1")
CODE_TTL = _i("SCA_CODE_TTL", 300)           # 验证码有效期（秒）
CODE_MAX_TRY = _i("SCA_CODE_MAX_TRY", 5)     # 连续答错上限，达到即锁定 10 分钟

# 这里**故意没有**"回显验证码"的开关：图形验证码的答案一旦随响应返回，
# 「用户识别图片内容」这一步就只剩形式，人机校验等于没做。
# 需要自动化测试时由测试脚本自己读 Redis（见 scripts/_captcha_solver.py）。

# ---- JWT ----------------------------------------------------------------
JWT_SECRET = _s("SCA_JWT_SECRET", "")
JWT_ALG = "HS256"
JWT_EXPIRE_MINUTES = _i("SCA_JWT_EXPIRE_MINUTES", 720)

# ---- 业务费率 ------------------------------------------------------------
# 公司统一规定，两侧各 2%；按订单金额（运费）比例计。改这里即可，不动代码。
SHIPPER_FEE_RATE = _f("SCA_SHIPPER_FEE_RATE", 0.02)
CARRIER_FEE_RATE = _f("SCA_CARRIER_FEE_RATE", 0.02)

# ---- 对话 Agent ----------------------------------------------------------
CHAT_MAX_ROUNDS = _i("SCA_CHAT_MAX_ROUNDS", 12)  # 单会话最大轮次（有界自主）
