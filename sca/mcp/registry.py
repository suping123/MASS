"""MCP Server 注册表（FR-11）：7 个 Server 覆盖 12 子系统。

用于接入层/看板展示"3 个核心 MCP / 7 个 Server / 12 子系统"的覆盖关系，
是需求 §11 工具层的证据化清单。
"""
from __future__ import annotations

from typing import Dict, List

# server -> 覆盖的子系统 + 提供的工具
MCP_REGISTRY: Dict[str, Dict] = {
    "tms-mcp":      {"subsystems": ["TMS", "OMS", "海关"],    "tools": ["query_order", "create_shipment", "update_status", "list_carriers"], "core": True},
    "gps-mcp":      {"subsystems": ["GPS/IoT", "系统"],       "tools": ["get_latest_position", "replay_track", "get_telemetry"], "core": True},
    "capacity-mcp": {"subsystems": ["运力"],                  "tools": ["match_capacity", "rank_carriers"], "core": True},
    "map-mcp":      {"subsystems": ["地图"],                  "tools": ["plan_route", "estimate_eta"], "core": False},
    "weather-mcp":  {"subsystems": ["天气"],                  "tools": ["get_forecast", "get_warning"], "core": False},
    "knowledge-mcp":{"subsystems": ["知识库", "WMS"],         "tools": ["search_knowledge", "list_sops"], "core": False},
    "notify-mcp":   {"subsystems": ["通知", "客户"],          "tools": ["send_dingtalk", "send_email"], "core": False},
}

# 12 子系统全覆盖校验
_ALL_SUBSYSTEMS = [
    "TMS", "OMS", "运力", "GPS/IoT", "地图", "天气", "知识库", "WMS",
    "系统", "通知", "客户", "海关",
]


def coverage_summary() -> Dict:
    covered = set()
    for meta in MCP_REGISTRY.values():
        covered.update(meta["subsystems"])
    missing = [s for s in _ALL_SUBSYSTEMS if s not in covered]
    core = [n for n, m in MCP_REGISTRY.items() if m["core"]]
    return {
        "servers": len(MCP_REGISTRY),
        "core_servers": core,
        "subsystems_covered": sorted(covered),
        "subsystems_total": len(_ALL_SUBSYSTEMS),
        "missing": missing,
        "all_covered": not missing,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(coverage_summary(), ensure_ascii=False, indent=2))
