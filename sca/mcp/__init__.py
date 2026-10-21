"""MCP 工具层（FR-11）包入口：7 个 Server 覆盖 12 子系统。

每个 Server 独立进程运行（FastMCP 标准协议）；原型期内置内存 mock，接真实系统时
替换数据源。注册表 registry.MCP_REGISTRY 给出 server→子系统→工具 的覆盖证据。
"""
from .registry import MCP_REGISTRY, coverage_summary
from .tms_server import mcp as tms_mcp, run as tms_run
from .gps_server import mcp as gps_mcp, run as gps_run
from .capacity_server import mcp as capacity_mcp, run as capacity_run
from .map_server import mcp as map_mcp, run as map_run
from .weather_server import mcp as weather_mcp, run as weather_run
from .knowledge_server import mcp as knowledge_mcp, run as knowledge_run
from .notify_server import mcp as notify_mcp, run as notify_run
from .carriers_store import load_carriers, upsert_carrier, delete_carrier

SERVERS = {
    "tms": (tms_mcp, tms_run),
    "gps": (gps_mcp, gps_run),
    "capacity": (capacity_mcp, capacity_run),
    "map": (map_mcp, map_run),
    "weather": (weather_mcp, weather_run),
    "knowledge": (knowledge_mcp, knowledge_run),
    "notify": (notify_mcp, notify_run),
}

__all__ = ["SERVERS", "MCP_REGISTRY", "coverage_summary", "load_carriers",
           "upsert_carrier", "delete_carrier"]
