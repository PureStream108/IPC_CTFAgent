from __future__ import annotations

import asyncio
from typing import Any

from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.tools.tool_registry import ToolRegistry


def build_tool_search_mcp(registry: ToolRegistry) -> MCPServer:
    server = create_mcp_server("tool_search", "Search for CTF tools across all categories")

    @server.tool(
        name="tool_search",
        description=(
            "Search the full tool catalog by keyword when the tool you need is not "
            "in your exposed category. Returns matching tools with how/when to use them."
        ),
    )
    async def tool_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
        tools = await asyncio.to_thread(registry.search, query, limit=limit)
        return [t.to_dict() for t in tools]

    return server


def build_category_tools_mcp(registry: ToolRegistry, category: str) -> MCPServer:
    server = create_mcp_server(
        "tools",
        f"Tools exposed for a {category} challenge plus how to invoke them",
    )
    exposed = registry.exposed_for(category)

    @server.tool(
        name="list_tools",
        description="List the tools currently exposed for this challenge category.",
    )
    async def list_tools() -> list[dict[str, Any]]:
        return [t.to_dict() for t in exposed]

    @server.tool(
        name="get_tool",
        description="Get the invocation command + path for a named tool.",
    )
    async def get_tool(name: str) -> dict[str, Any]:
        tool = registry.get(name)
        if tool is None:
            return {"error": f"no tool named {name}"}
        return {"name": tool.name, "exec": tool.exec, "path": tool.path, "when_to_use": tool.when_to_use}

    return server
