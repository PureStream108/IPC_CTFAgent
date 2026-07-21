from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

MCPServer = FastMCP
MCPTransport = Literal["stdio", "sse", "streamable-http"]

SERVER_NAMES = (
    "browser",
    "ghidra",
    "memory",
    "tool_search",
    "tools",
    "zap",
)


def create_mcp_server(name: str, description: str = "") -> MCPServer:

    return FastMCP(name=name, instructions=description or None)


def build_mcp_server(name: str, root: str | Path = ".", category: str = "misc") -> MCPServer:
    """Build one IPC MCP server for an external stdio/HTTP client."""

    root_path = Path(root)
    if name == "browser":
        from backend.mcp.shared import build_browser_mcp

        return build_browser_mcp()
    if name == "ghidra":
        from backend.mcp.shared import build_ghidra_mcp

        return build_ghidra_mcp()
    if name == "zap":
        from backend.mcp.shared import build_zap_mcp

        return build_zap_mcp()

    data_dir = root_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if name == "memory":
        from backend.memory.memory_mcp import build_memory_mcp
        from backend.memory.memory_store import MemoryStore

        store = MemoryStore(data_dir / "memory.db", export_dir=root_path / "memory").configure()
        return build_memory_mcp(store)
    if name in {"tool_search", "tools"}:
        from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
        from backend.tools.tool_registry import ToolRegistry

        registry = ToolRegistry(cache_db=data_dir / "tool_cache.db").load()
        if name == "tools":
            return build_category_tools_mcp(registry, category)
        return build_tool_search_mcp(registry)
    raise ValueError(f"unknown MCP server: {name}")


def run_mcp_server(
    server: MCPServer,
    transport: MCPTransport = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run a FastMCP server using the selected SDK transport."""

    server.settings.host = host
    server.settings.port = port
    server.run(transport=transport)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an IPC MCP server")
    parser.add_argument("server", choices=SERVER_NAMES)
    parser.add_argument("--root", default=".", help="IPC runtime root for stateful MCP servers")
    parser.add_argument("--category", default="misc", help="challenge category for the tools server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_mcp_server(args.server, root=args.root, category=args.category)
    run_mcp_server(server, args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
