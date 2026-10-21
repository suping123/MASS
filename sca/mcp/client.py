"""MCP 工具调用客户端（FR-11 工具层真实调用链路）。

职责：
  * 统一入口 call(server, tool, **kwargs) -> ToolResult，让 agent 节点不再直接
    依赖本地 mock 池，而是"真正发起一次 MCP 工具调用"；
  * 每次调用留证据（server / tool / 参数 / 耗时 / 来源 / 是否降级），供看板与审计；
  * 传输可替换：默认进程内直连（零依赖，原型期）；装了官方 mcp 包或配置了
    SCA_MCP_HTTP_BASE 时，可切换到 stdio / HTTP 真实 MCP 传输（接口不变）。

ToolResult.data 为工具返回值；ToolResult.ok=False 时调用方应降级并保留 error。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sca.mcp.client")

# server 模块 -> (模块路径, 工具函数映射)
_SERVER_MODULES: Dict[str, str] = {
    "capacity-mcp": "sca.mcp.capacity_server",
    "map-mcp": "sca.mcp.map_server",
    "tms-mcp": "sca.mcp.tms_server",
    "gps-mcp": "sca.mcp.gps_server",
    "weather-mcp": "sca.mcp.weather_server",
    "knowledge-mcp": "sca.mcp.knowledge_server",
    "notify-mcp": "sca.mcp.notify_server",
}

# 调用证据（环形保留最近 N 条，供看板/审计展示）
_CALLS: List[Dict[str, Any]] = []
_MAX_CALLS = 200


@dataclass
class ToolResult:
    ok: bool
    server: str
    tool: str
    data: Any = None
    error: str = ""
    latency_ms: float = 0.0
    transport: str = "inproc"
    degraded: bool = False
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "server": self.server, "tool": self.tool,
                "latency_ms": round(self.latency_ms, 2), "transport": self.transport,
                "degraded": self.degraded, "args": self.args,
                "data": self.data, "error": self.error}


def _http_base() -> str:
    return os.environ.get("SCA_MCP_HTTP_BASE", "").strip()


async def _call_http(server: str, tool: str, kwargs: Dict[str, Any]) -> Any:
    """HTTP 传输（真实 MCP 网关 / 自建工具服务）：POST {base}/{server}/{tool}。"""
    import json
    import urllib.request

    from sca.util.aio import fetch_bytes

    url = f"{_http_base().rstrip('/')}/{server}/{tool}"
    req = urllib.request.Request(
        url, data=json.dumps(kwargs).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads((await fetch_bytes(req, timeout=8)).decode("utf-8"))


async def call(server: str, tool: str, **kwargs: Any) -> ToolResult:
    """发起一次 MCP 工具调用（异步）。失败不抛异常，返回 ok=False 供调用方降级。"""
    t0 = time.time()
    base = _http_base()
    try:
        if base:
            data = await _call_http(server, tool, kwargs)
            transport = "http"
        else:
            mod_path = _SERVER_MODULES.get(server)
            if not mod_path:
                raise RuntimeError(f"未知 MCP Server: {server}")
            import importlib
            mod = importlib.import_module(mod_path)
            fn = getattr(mod, tool, None)
            if fn is None:
                raise RuntimeError(f"{server} 未提供工具 {tool}")
            data = await fn(**kwargs) if callable(fn) else fn
            transport = "inproc"
        res = ToolResult(ok=True, server=server, tool=tool, data=data,
                         latency_ms=(time.time() - t0) * 1000, transport=transport,
                         args=kwargs)
    except Exception as exc:  # 工具不可用 -> 降级
        logger.warning("[mcp] %s.%s 调用失败：%s", server, tool, exc)
        res = ToolResult(ok=False, server=server, tool=tool,
                         error=str(exc), latency_ms=(time.time() - t0) * 1000,
                         transport="http" if base else "inproc", args=kwargs)
    _record(res)
    return res


def _record(res: ToolResult) -> None:
    _CALLS.append(res.to_dict())
    if len(_CALLS) > _MAX_CALLS:
        del _CALLS[: len(_CALLS) - _MAX_CALLS]


def recent_calls(limit: int = 20) -> List[Dict[str, Any]]:
    """最近 N 次 MCP 调用证据（看板/审计用）。"""
    return list(reversed(_CALLS[-limit:]))


def call_stats() -> Dict[str, Any]:
    if not _CALLS:
        return {"total": 0, "by_server": {}, "failures": 0, "avg_latency_ms": 0}
    by_server: Dict[str, int] = {}
    fails = 0
    lat = 0.0
    for c in _CALLS:
        by_server[c["server"]] = by_server.get(c["server"], 0) + 1
        if not c["ok"]:
            fails += 1
        lat += c["latency_ms"]
    return {"total": len(_CALLS), "by_server": by_server, "failures": fails,
            "avg_latency_ms": round(lat / len(_CALLS), 2),
            "transport": _CALLS[-1]["transport"]}
