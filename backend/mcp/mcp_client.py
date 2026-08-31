from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

MCPClientTarget = FastMCP | StdioServerParameters


class MCPToolError(RuntimeError):
    """Raised when an MCP server reports a failed tool invocation."""


def _decode_tool_result(result: types.CallToolResult) -> Any:
    if result.isError:
        message = "\n".join(
            block.text for block in result.content if isinstance(block, types.TextContent)
        ) or "MCP tool call failed"
        raise MCPToolError(message)

    if result.structuredContent is not None:
        structured = result.structuredContent
        if set(structured) == {"result"}:
            return structured["result"]
        return structured

    content = result.content
    if len(content) == 1 and isinstance(content[0], types.TextContent):
        text = content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in content]


class MCPClient:
    """Async MCP client with a reusable official ``ClientSession``."""

    def __init__(
        self,
        target: MCPClientTarget,
        *,
        read_timeout: float | None = 120,
    ) -> None:
        self.target = target
        self.read_timeout = read_timeout
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @classmethod
    def in_process(cls, server: FastMCP, **kwargs: Any) -> MCPClient:
        return cls(server, **kwargs)

    @classmethod
    def stdio(
        cls,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        **kwargs: Any,
    ) -> MCPClient:
        return cls(
            StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd),
            **kwargs,
        )

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCPClient must be used as an async context manager")
        return self._session

    async def __aenter__(self) -> MCPClient:
        if self._stack is not None:
            raise RuntimeError("MCPClient session is already open")
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            timeout = timedelta(seconds=self.read_timeout) if self.read_timeout is not None else None
            if isinstance(self.target, FastMCP):
                session = await stack.enter_async_context(
                    create_connected_server_and_client_session(
                        self.target,
                        read_timeout_seconds=timeout,
                    )
                )
            else:
                read_stream, write_stream = await stack.enter_async_context(stdio_client(self.target))
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream, read_timeout_seconds=timeout)
                )
                await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            page = await self.session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools

    async def describe_tools(self) -> list[dict[str, Any]]:
        return [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in await self.list_tools()
        ]

    async def call_tool_result(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        return await self.session.call_tool(name, arguments=arguments or {})

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return _decode_tool_result(await self.call_tool_result(name, arguments))


MCPRegistryTarget = MCPClientTarget | MCPClient


class MCPRegistrySession:
    """Opens and reuses one async client session per named server."""

    def __init__(self, targets: Mapping[str, MCPRegistryTarget]) -> None:
        self._targets = dict(targets)
        self._stack = AsyncExitStack()
        self._clients: dict[str, MCPClient] = {}
        self._open = False

    async def __aenter__(self) -> MCPRegistrySession:
        await self._stack.__aenter__()
        self._open = True
        # Servers backed by ``docker exec`` are comparatively expensive to
        # initialise.  Open only the server a Member actually calls instead of
        # spawning every browser/reverse/tool process before its first action.
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._open = False
        self._clients.clear()
        await self._stack.__aexit__(exc_type, exc, tb)

    def names(self) -> list[str]:
        return list(self._targets)

    async def _client(self, server: str) -> MCPClient:
        if not self._open:
            raise RuntimeError("MCP registry session is not open")
        client = self._clients.get(server)
        if client is None:
            target = self._targets.get(server)
            if target is None:
                raise KeyError(f"no MCP server named '{server}'")
            candidate = target if isinstance(target, MCPClient) else MCPClient(target)
            client = await self._stack.enter_async_context(candidate)
            self._clients[server] = client
        return client

    async def list_tools(self, server: str) -> list[types.Tool]:
        return await (await self._client(server)).list_tools()

    async def describe_tools(self, server: str) -> list[dict[str, Any]]:
        return await (await self._client(server)).describe_tools()

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        return await (await self._client(server)).call_tool(tool, arguments)


class MCPRegistry:
    """Registry of MCP server definitions or remote transport targets."""

    def __init__(self) -> None:
        self._targets: dict[str, MCPRegistryTarget] = {}

    def register(self, server: FastMCP, name: str | None = None) -> None:
        self.register_target(name or server.name, server)

    def register_target(self, name: str, target: MCPRegistryTarget) -> None:
        self._targets[name] = target

    def get(self, name: str) -> MCPRegistryTarget | None:
        return self._targets.get(name)

    def names(self) -> list[str]:
        return list(self._targets)

    @asynccontextmanager
    async def session(
        self,
        extra_servers: Mapping[str, MCPRegistryTarget] | None = None,
    ) -> AsyncIterator[MCPRegistrySession]:
        targets = {**self._targets, **dict(extra_servers or {})}
        async with MCPRegistrySession(targets) as session:
            yield session

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        async with self.session() as session:
            return await session.call_tool(server, tool, arguments)


async def _run_cli(args: argparse.Namespace) -> int:
    server_args = [
        "-m",
        "backend.mcp.mcp_server",
        args.server,
        "--root",
        args.root,
        "--category",
        args.category,
    ]
    client = MCPClient.stdio(sys.executable, server_args, cwd=Path.cwd())
    async with client:
        if args.tool:
            arguments = json.loads(args.arguments)
            result = await client.call_tool(args.tool, arguments)
        else:
            result = await client.describe_tools()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call an IPC MCP server through an async stdio session")
    parser.add_argument("server", choices=("browser", "reverse", "memory", "tool_search", "tools", "zap"))
    parser.add_argument("tool", nargs="?")
    parser.add_argument("--arguments", default="{}", help="JSON object passed to the tool")
    parser.add_argument("--root", default=".")
    parser.add_argument("--category", default="misc")
    args = parser.parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
