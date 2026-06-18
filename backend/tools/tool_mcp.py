from __future__ import annotations

from backend.mcp.base import MCPServer
from backend.tools.tool_registry import ToolRegistry


def build_tool_search_mcp(registry: ToolRegistry) -> MCPServer:
    server = MCPServer(name="tool_search", description="Search for CTF tools across all categories")

    @server.tool(
        name="tool_search",
        description=(
            "Search the full tool catalog by keyword when the tool you need is not "
            "in your exposed category. Returns matching tools with how/when to use them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "keywords, e.g. 'rsa lattice' or 'memory forensics'"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    )
    def tool_search(query: str, limit: int = 8):
        tools = registry.search(query, limit=limit)
        return [t.to_dict() for t in tools]

    return server


def build_category_tools_mcp(registry: ToolRegistry, category: str) -> MCPServer:
    server = MCPServer(
        name="tools",
        description=f"Tools exposed for a {category} challenge plus how to invoke them",
    )
    exposed = registry.exposed_for(category)

    @server.tool(
        name="list_tools",
        description="List the tools currently exposed for this challenge category.",
        input_schema={"type": "object", "properties": {}},
    )
    def list_tools():
        return [t.to_dict() for t in exposed]

    @server.tool(
        name="get_tool",
        description="Get the invocation command + path for a named tool.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    def get_tool(name: str):
        tool = registry.get(name)
        if tool is None:
            return {"error": f"no tool named {name}"}
        return {"name": tool.name, "exec": tool.exec, "path": tool.path, "when_to_use": tool.when_to_use}

    return server
