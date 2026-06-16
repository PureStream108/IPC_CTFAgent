from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def call(self, **kwargs: Any) -> Any:
        return self.handler(**kwargs)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(slots=True)
class MCPServer:
    name: str
    description: str = ""
    tools: dict[str, MCPTool] = field(default_factory=dict)

    def add_tool(self, tool: MCPTool) -> None:
        self.tools[tool.name] = tool

    def tool(self, name: str, description: str, input_schema: dict[str, Any]) -> Callable:
        """Decorator to register a handler as a tool."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add_tool(MCPTool(name=name, description=description, input_schema=input_schema, handler=fn))
            return fn

        return deco

    def call(self, tool_name: str, **kwargs: Any) -> Any:
        if tool_name not in self.tools:
            raise KeyError(f"MCP server '{self.name}' has no tool '{tool_name}'")
        return self.tools[tool_name].call(**kwargs)

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.describe() for t in self.tools.values()]


class MCPRegistry:

    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}

    def register(self, server: MCPServer) -> None:
        self._servers[server.name] = server

    def get(self, name: str) -> MCPServer | None:
        return self._servers.get(name)

    def names(self) -> list[str]:
        return list(self._servers)

    def call(self, server: str, tool: str, **kwargs: Any) -> Any:
        srv = self._servers.get(server)
        if srv is None:
            raise KeyError(f"no MCP server named '{server}'")
        return srv.call(tool, **kwargs)

    def describe(self) -> dict[str, list[dict[str, Any]]]:
        return {name: srv.list_tools() for name, srv in self._servers.items()}
