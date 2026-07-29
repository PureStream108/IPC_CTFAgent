from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP

MCPServer = FastMCP
MCPTransport = Literal["stdio", "sse", "streamable-http"]

SERVER_NAMES = (
    "browser",
    "reverse",
    "memory",
    "tool_search",
    "tools",
    "zap",
)


def create_mcp_server(
    name: str,
    description: str = "",
    *,
    lifespan: Callable[[FastMCP], Any] | None = None,
) -> MCPServer:

    return FastMCP(name=name, instructions=description or None, lifespan=lifespan)


def close_lifespan(close: Callable[[], None]):
    """Build a FastMCP lifespan for a resource owned by one server."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            close()

    return lifespan


def build_mcp_server(name: str, root: str | Path = ".", category: str = "misc") -> MCPServer:
    """Build one IPC MCP server for an external stdio/HTTP client."""

    root_path = Path(root)
    if name == "browser":
        from backend.mcp.shared import build_browser_mcp

        return build_browser_mcp()
    if name == "reverse":
        from backend.mcp.reverse_mcp import build_reverse_mcp

        return build_reverse_mcp()
    if name == "zap":
        from backend.mcp.shared import build_zap_mcp

        return build_zap_mcp()

    # Parity with AppState: these standalone debug servers keep their state in
    # RAM too, so the paths only name the in-RAM databases.
    data_dir = root_path / "data"
    if name == "memory":
        from backend.memory.memory_mcp import build_memory_mcp
        from backend.memory.memory_store import MemoryStore
        from backend.tools.catalog import ToolCatalog

        store = MemoryStore(data_dir / "memory.db", export_dir=None, in_memory=True).configure()
        return build_memory_mcp(
            store,
            catalog=ToolCatalog.load(),
            lifespan=close_lifespan(store.close),
        )
    if name in {"tool_search", "tools"}:
        from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
        from backend.tools.tool_registry import ToolRegistry

        registry = ToolRegistry(cache_db=data_dir / "tool_cache.db", in_memory=True).load()
        if name == "tools":
            return build_category_tools_mcp(
                registry, category, lifespan=close_lifespan(registry.close)
            )
        return build_tool_search_mcp(
            registry, lifespan=close_lifespan(registry.close)
        )
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
