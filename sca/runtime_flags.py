"""运行时能力开关（进程内）。

为什么单独一个模块：调度内核 `sca.agents` 与平台层 `sca.platform` 相互独立，
谁都不该 import 对方。配置的"写入方"是平台层（管理页面），"读取方"是内核
（决定这次要不要调模型），两边共用一个中立的开关容器。

语义：
  * `None`  = 未设置，按各自默认（允许调模型）；
  * `True`  = 允许；
  * `False` = 禁止，走确定性算法。
未设置时保持既有行为，避免"平台层没启动时内核莫名其妙不调模型"。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

_FLAGS: Dict[str, Optional[bool]] = {
    "route_model": None,    # 选路是否允许用模型（plan_route_smart）
    "intent_model": None,   # 意图识别是否允许用模型
}

# 最近一次同步的时间与来源，便于页面显示"生效于何时"
_META: Dict[str, Any] = {"synced_at": None, "source": ""}


def get(name: str, default: bool = True) -> bool:
    v = _FLAGS.get(name)
    return default if v is None else bool(v)


def set_flags(**kw: Optional[bool]) -> None:
    for k, v in kw.items():
        if k in _FLAGS:
            _FLAGS[k] = None if v is None else bool(v)


def snapshot() -> Dict[str, Any]:
    return {**_FLAGS, **_META}


def mark_synced(source: str = "db") -> None:
    from datetime import datetime

    _META["synced_at"] = datetime.now().isoformat(timespec="seconds")
    _META["source"] = source


def apply_from_config(conf: Dict[str, Any], source: str = "db") -> None:
    """把 `model.enabled` 配置映射成开关。缺字段时保持 None（沿用默认）。"""
    if not isinstance(conf, dict):
        return
    set_flags(route_model=conf.get("route"), intent_model=conf.get("intent"))
    mark_synced(source)
