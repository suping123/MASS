"""运行时配置：让管理页面改的东西**真的生效**，而不是只存进数据库。

为什么需要这一层：
  服务费率、模型开关这些值原先来自 `.env`（启动时读一次），改库不会影响已启动的
  进程，页面就会变成"改了没反应"的摆设。这里提供一个带短 TTL 的读取入口，
  数据库为准、`.env` 为兜底，30 秒内自动感知变更。

写入只走 `system_config` / `fee_rule` 两张表；`.env` 不被改写（避免两个真相源）。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config as cfg
from ..models import FeeRule, SystemConfig

_TTL = 30.0
# 费率和系统配置是**两个缓存**，各用各的时间戳。
# 曾经两者共用一个 "at"：只要有人读了一次系统配置（`all_config` 里刷新时间戳），
# 费率缓存的"新鲜度"就被顺带续上了 —— 刚新建完 3% 的费率规则，
# `active_fee` 仍认为手里那份 2% 是新鲜的，直接返回，连库都不查。
# 现象是"改了费率，下一单还是按旧费率挂牌"，而且能不能复现取决于调用顺序，
# 排查时看着像随机 flake。下面两个 `*_at` 就是为了不让它们再互相续命。
_cache: Dict[str, Any] = {"fee": None, "fee_at": 0.0, "conf": {}, "conf_at": 0.0}


def invalidate() -> None:
    """配置变更后调用，立刻丢弃缓存（管理页面保存后应该马上看到新值）。"""
    _cache["fee_at"] = 0.0
    _cache["conf_at"] = 0.0


def _fresh(at_key: str) -> bool:
    return (time.time() - float(_cache[at_key] or 0)) < _TTL


def active_fee(db: Session) -> Tuple[Decimal, Decimal]:
    """当前生效的服务费率 (货主侧, 承运商侧)。库里没有就回落到 .env 的配置。"""
    if _fresh("fee_at") and _cache.get("fee"):
        return _cache["fee"]
    sr = Decimal(str(cfg.SHIPPER_FEE_RATE))
    cr = Decimal(str(cfg.CARRIER_FEE_RATE))
    try:
        row = db.execute(select(FeeRule).where(FeeRule.status == "active")
                         .order_by(FeeRule.effective_from.desc(), FeeRule.id.desc())
                         ).scalars().first()
        if row:
            sr, cr = Decimal(str(row.shipper_rate)), Decimal(str(row.carrier_rate))
    except Exception:  # noqa: BLE001 表还没建时按 .env 走，不影响业务
        pass
    _cache["fee"] = (sr, cr)
    _cache["fee_at"] = time.time()
    return sr, cr


def all_config(db: Session) -> Dict[str, Any]:
    if _fresh("conf_at") and _cache.get("conf"):
        return _cache["conf"]
    out: Dict[str, Any] = {}
    try:
        for row in db.execute(select(SystemConfig)).scalars().all():
            out[row.cfg_key] = row.cfg_value
    except Exception:  # noqa: BLE001
        out = {}
    _cache["conf"] = out
    _cache["conf_at"] = time.time()
    return out


def get(db: Session, key: str, default: Any = None) -> Any:
    return all_config(db).get(key, default)


def model_enabled(db: Session, capability: str) -> bool:
    """某能力是否允许调用模型。读不到配置时按"允许"处理（保持既有行为）。"""
    conf = get(db, "model.enabled") or {}
    if not isinstance(conf, dict):
        return True
    return bool(conf.get(capability, True))


def model_chain(db: Session, capability: str) -> list:
    conf = get(db, "model.chain") or {}
    if not isinstance(conf, dict):
        return []
    chain = conf.get(capability)
    return list(chain) if isinstance(chain, list) else []
