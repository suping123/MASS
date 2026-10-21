"""MCP 工具层（FR-11）：TMS 运单 Server（JSON 持久化）。

真实能力：运单创建/查询/状态流转均落盘 ``data/tms_orders.json``，跨进程、重启不丢。
原型期与生产 TMS 的契约差异只在数据源（换真实 TMS API 时改本文件的 IO 层，节点不动）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sca.mcp._compat import FastMCP, HAVE_MCP, _StubMCP

logger = logging.getLogger("sca.mcp.tms")

mcp = FastMCP("tms-mcp") if HAVE_MCP else _StubMCP()

_DB_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "tms_orders.json"
_ORDERS: Dict[str, Dict[str, Any]] = {}

# 合法状态机（防非法跳转）
_STATUS_FLOW = {
    "created": ["dispatched"],
    "dispatched": ["in_transit", "rerouted", "exception", "delivered"],
    "in_transit": ["rerouted", "exception", "delivered"],
    "rerouted": ["in_transit", "exception", "delivered"],
    "exception": ["in_transit", "delivered"],
    "delivered": [],
}


def _load() -> None:
    if _DB_FILE.is_file():
        try:
            _ORDERS.update(json.loads(_DB_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa
            logger.warning("[tms] 运单文件读取失败，从空库开始：%s", exc)


def _save() -> None:
    try:
        _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DB_FILE.write_text(json.dumps(_ORDERS, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except Exception as exc:  # noqa
        logger.warning("[tms] 运单落盘失败：%s", exc)


_load()


@mcp.tool()
async def create_shipment(order_id: str, carrier_id: str, eta: str,
                          route: str | None = None, distance_km: float | None = None,
                          cost_est: float | None = None) -> dict:
    """创建运单并下发至承运商（真实落盘，返回带时间戳的运单回执）。"""
    if order_id in _ORDERS and _ORDERS[order_id].get("status") != "created":
        return {"ok": False, "error": f"运单 {order_id} 已存在（status="
                f"{_ORDERS[order_id].get('status')}），拒绝重复下发"}
    rec = {
        "order_id": order_id, "carrier_id": carrier_id, "eta": eta,
        "route": route, "distance_km": distance_km, "cost_est": cost_est,
        "status": "dispatched", "created_at": int(time.time()),
        "status_history": [{"status": "dispatched", "ts": int(time.time())}],
    }
    _ORDERS[order_id] = rec
    _save()
    logger.info("[tms] 运单下发 %s -> 承运商 %s", order_id, carrier_id)
    return {"ok": True, **rec}


@mcp.tool()
async def query_order(order_id: str) -> dict:
    """查询运单状态（含状态流转历史）。"""
    rec = _ORDERS.get(order_id)
    if not rec:
        return {"ok": False, "error": "not_found", "order_id": order_id}
    return {"ok": True, **rec}


@mcp.tool()
async def update_status(order_id: str, status: str) -> dict:
    """更新运单状态（在途/改道/异常/签收），校验合法流转并落盘历史。"""
    rec = _ORDERS.get(order_id)
    if not rec:
        return {"ok": False, "error": "not_found", "order_id": order_id}
    cur = rec.get("status")
    allowed = _STATUS_FLOW.get(cur, [])
    if status not in allowed:
        return {"ok": False, "error": f"非法状态流转 {cur} -> {status}（允许：{allowed}）"}
    rec["status"] = status
    rec.setdefault("status_history", []).append({"status": status, "ts": int(time.time())})
    _save()
    return {"ok": True, **rec}


@mcp.tool()
async def update_plan(order_id: str, route: str | None = None,
                      distance_km: float | None = None, eta: str | None = None,
                      cost_est: float | None = None, reason: str | None = None) -> dict:
    """改道后更新运单计划（状态转 rerouted，保留历史版本）。"""
    rec = _ORDERS.get(order_id)
    if not rec:
        return {"ok": False, "error": "not_found", "order_id": order_id}
    rec.setdefault("plan_history", []).append(
        {"route": rec.get("route"), "distance_km": rec.get("distance_km"),
         "eta": rec.get("eta"), "cost_est": rec.get("cost_est"),
         "ts": int(time.time())})
    for k, v in (("route", route), ("distance_km", distance_km),
                 ("eta", eta), ("cost_est", cost_est)):
        if v is not None:
            rec[k] = v
    if reason:
        rec["last_reroute_reason"] = reason
    if rec.get("status") in ("dispatched", "in_transit"):
        rec["status"] = "rerouted"
        rec.setdefault("status_history", []).append(
            {"status": "rerouted", "ts": int(time.time())})
    _save()
    return {"ok": True, **rec}


@mcp.tool()
async def list_orders(status: str | None = None, limit: int = 20) -> list:
    """按状态列出运单（最近优先）。"""
    items = [v for v in _ORDERS.values() if status is None or v.get("status") == status]
    items.sort(key=lambda x: -x.get("created_at", 0))
    return items[:limit]


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
