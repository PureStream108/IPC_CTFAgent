from __future__ import annotations

import asyncio

import pytest

from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import SERVER_NAMES, build_mcp_server
from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
from backend.tools.tool_registry import ToolRegistry


def call_tool(server, tool: str, **arguments):
    async def run():
        async with MCPClient.in_process(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run())


@pytest.fixture
def registry():
    return ToolRegistry().load()


def test_registry_loads_all_categories(registry):
    cats = registry.categories()
    for category in ("web", "reverse", "crypto", "pwn", "misc", "ai", "osint"):
        assert category in cats
    assert registry.get("sqlmap") is not None


def test_exposed_for_category(registry):
    web = registry.exposed_for("web")
    names = {tool.name for tool in web}
    assert {"playwright-cli", "sqlmap", "flask-pin"} <= names
    assert all(tool.category == "web" for tool in web)
    assert all(tool.description and tool.exec and tool.when_to_use for tool in web)
    assert not {"browser", "fenjing", "typhonbreaker"} & names


def test_removed_tools_are_not_exposed_or_searchable(registry):
    removed = {"browser", "reverse", "ghidra", "fenjing", "typhonbreaker",
               "php-filter-chain", "jndimap", "shiro550", "rsactftool",
               "rsa-sign2n", "sage", "flask-unsign", "flask-session-cookie",
               "blindwatermark"}
    for name in removed:
        assert registry.get(name) is None
    for query in ("browser playwright mcp", "ghidra decompile", "fenjing ssti",
                  "typhonbreaker pyjail", "jndimap log4shell", "rsa weak modulus",
                  "sage lattice factoring", "flask unsign secret", "blind watermark"):
        hits = {tool.name for tool in registry.search(query)}
        assert not hits & removed


def test_new_cli_tools_are_searchable(registry):
    for query, expected in (
        ("playwright browser screenshot", "playwright-cli"),
        ("flask werkzeug debugger pin", "flask-pin"),
        ("upx unpack packed binary", "upx"),
        ("pcap packet capture wireshark", "tshark"),
        ("syscall trace linux", "strace"),
        ("remote gdb debug", "gdbserver"),
        ("carve files disk image", "foremost"),
        ("elf dwarf readelf", "elfutils"),
        ("socket relay tunnel", "socat"),
    ):
        names = {tool.name for tool in registry.search(query)}
        assert expected in names, (query, names)


def test_cli_registry_executables(registry):
    for name, executable in (
        ("playwright-cli", "playwright-cli"),
        ("flask-pin", "python get_flask_pin_and_cookie.py"),
        ("upx", "upx"),
        ("tshark", "tshark"),
        ("strace", "strace"),
        ("gdbserver", "gdbserver"),
        ("foremost", "foremost"),
        ("elfutils", "eu-readelf"),
        ("socat", "socat"),
        ("radare2", "r2"),
        ("gdb", "gdb"),
    ):
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.exec == executable


def test_unavailable_mcp_tools_are_filtered(registry):
    assert registry.get("zap", available_mcps=set()) is None
    assert not registry.search("zap spider", available_mcps=set())

    mcp = build_category_tools_mcp(registry, "web", available_mcps=set())
    assert "zap" not in {tool["name"] for tool in call_tool(mcp, "list_tools")}
    assert call_tool(mcp, "get_tool", name="zap")["error"]

    search_mcp = build_tool_search_mcp(registry, available_mcps=set())
    assert not call_tool(search_mcp, "tool_search", query="zap spider")


def test_tool_search_finds_cross_category(registry):
    results = registry.search("rsa lattice factoring")
    names = {tool.name for tool in results}
    assert "cado-nfs" in names


def test_tool_search_finds_flask_helper(registry):
    results = registry.search("flask session cookie secret")
    assert "flask-pin" in {tool.name for tool in results}


def test_tool_search_cache(registry):
    registry.search("memory forensics")
    cached = registry.cached_search("memory forensics")
    assert cached is not None
    assert "volatility3" in cached


def test_tool_search_mcp(registry):
    mcp = build_tool_search_mcp(registry)
    hits = call_tool(mcp, "tool_search", query="flask cookie secret")
    assert any(hit["name"] == "flask-pin" for hit in hits)


def test_category_tools_mcp(registry):
    mcp = build_category_tools_mcp(registry, "pwn")
    tools = call_tool(mcp, "list_tools")
    assert any(tool["name"] == "pwntools" for tool in tools)
    assert call_tool(mcp, "get_tool", name="gdb")["exec"] == "gdb"
    assert call_tool(mcp, "get_tool", name="nope")["error"]


def test_category_tools_mcp_returns_tool_contract(registry):
    mcp = build_category_tools_mcp(registry, "web")
    listed = call_tool(mcp, "list_tools")
    playwright = next(tool for tool in listed if tool["name"] == "playwright-cli")
    assert set(playwright) == {"name", "category", "description", "exec", "tags", "when_to_use"}
    detail = call_tool(mcp, "get_tool", name="playwright-cli")
    assert detail["exec"] == playwright["exec"]
    assert detail["when_to_use"] == playwright["when_to_use"]


def test_server_names_only_contain_supported_mcp_servers():
    assert SERVER_NAMES == ("memory", "tool_search", "tools")
    for removed in ("browser", "reverse", "ghidra"):
        assert removed not in SERVER_NAMES
        with pytest.raises(ValueError, match="unknown MCP server"):
            build_mcp_server(removed)
