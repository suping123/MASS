"""承运商（运力）配置持久化。

数据源：``data/carriers.json``（首次运行从 capacity_server 内置 _CARRIERS 初始化并落盘）。
前端"承运商"模块与 capacity-mcp 的 _pool() 都从此读取，保证配置唯一来源、可热改。

字段与 capacity_server._CARRIERS 保持一致：
  id / name / vehicle / capacity_t / temp_ctrl / hazmat / score /
  cost_per_km / city / on_time_rate / fleet
  driver_name / driver_phone（司机联系人，用于钉钉群内 @ 到司机本人；
    内置承运商默认为空——手机号属真实个人信息，必须由用户填写，系统不编造）
  order_id / idle（运力占用，2026-10-15 用户要求）：
    order_id = 当前正在承运的运单号（空 = 未接单）；
    idle     = 是否空闲可派。**有在途订单必然非空闲**（order_id 非空 -> idle=False），
               可手动置 False 表示车辆被占用但未挂单（如维修/场内作业）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_PATH = Path(__file__).resolve().parents[2] / "data" / "carriers.json"
_LOCK = threading.Lock()


def _default_carriers() -> List[Dict[str, Any]]:
    # 延迟导入，避免与 capacity_server 形成循环依赖
    from sca.mcp.capacity_server import _CARRIERS
    return [dict(c) for c in _CARRIERS]


# 新字段的默认值（司机联系人留空等待用户填写，不自动生成号码）
_RECORD_DEFAULTS = {"driver_name": "", "driver_phone": "", "order_id": "", "idle": True}


def _normalize(c: Dict[str, Any]) -> Dict[str, Any]:
    """补默认字段 + 维持「承运订单号 / 是否空闲」一致：有在途订单必然非空闲。"""
    for k, v in _RECORD_DEFAULTS.items():
        c.setdefault(k, v)
    c["order_id"] = str(c.get("order_id") or "").strip()
    if c["order_id"]:
        c["idle"] = False
    c["idle"] = bool(c["idle"])
    return c


def _load() -> List[Dict[str, Any]]:
    if _PATH.is_file():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                changed = False
                for c in data:
                    before = len(c)
                    _normalize(c)
                    if len(c) != before:   # 补了旧数据缺的新字段 -> 回写一次，磁盘与内存一致
                        changed = True
                if changed:
                    _save(data)
                return data
        except Exception:  # 损坏则回退默认
            pass
    data = [_normalize(dict(c)) for c in _default_carriers()]
    _save(data)
    return data


def _save(data: List[Dict[str, Any]]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def load_carriers() -> List[Dict[str, Any]]:
    """返回当前承运商列表（线程安全）。"""
    with _LOCK:
        return _load()


def upsert_carrier(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按 id 新增或覆盖一个承运商；缺 id 时自动生成 C 编号。返回落盘后的记录。"""
    with _LOCK:
        data = _load()
        c = dict(c or {})
        cid = c.get("id")
        if not cid:
            n = len(data) + 1
            while any(x.get("id") == f"C{n:03d}" for x in data):
                n += 1
            cid = f"C{n:03d}"
            c = {**c, "id": cid}
        for i, x in enumerate(data):
            if x.get("id") == cid:
                data[i] = _normalize({**x, **c, "id": cid})
                _save(data)
                return data[i]
        c = _normalize(c)
        data.append(c)
        _save(data)
        return c


def delete_carrier(cid: str) -> bool:
    """删除指定 id 的承运商；返回是否删除成功。"""
    with _LOCK:
        data = _load()
        new = [x for x in data if x.get("id") != cid]
        if len(new) == len(data):
            return False
        _save(new)
        return True
