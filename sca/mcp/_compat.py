"""MCP 兼容层：未安装 mcp 包时安全降级。

装了官方 `mcp` 包则直接用 FastMCP；否则提供 _StubMCP，使 @mcp.tool() 退化为
普通函数装饰器、mcp.run() 退化为空操作——保证整套系统在不联网安装依赖时也能
导入与运行（MCP 服务仅不真正起 Server，注册表与覆盖信息不受影响）。
"""
from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP
    HAVE_MCP = True
except Exception:  # 未安装 mcp 包
    FastMCP = None
    HAVE_MCP = False

    class _StubMCP:
        """占位 MCP Server：tool 装饰器原样返回函数，run 为空操作。"""

        def tool(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

        def run(self, *args, **kwargs):
            print("[mcp] mcp 包未安装，MCP Server 未启动（不影响其余功能）")
