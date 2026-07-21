from __future__ import annotations

from typing import Any

__all__ = [
    "MCPClient",
    "MCPRegistry",
    "MCPRegistrySession",
    "MCPServer",
    "MCPToolError",
    "create_mcp_server",
]


def __getattr__(name: str) -> Any:
    """Load public symbols lazily so module-based server entry points stay clean."""

    if name in {"MCPClient", "MCPRegistry", "MCPRegistrySession", "MCPToolError"}:
        from backend.mcp import mcp_client

        return getattr(mcp_client, name)
    if name in {"MCPServer", "create_mcp_server"}:
        from backend.mcp import mcp_server

        return getattr(mcp_server, name)
    raise AttributeError(name)
