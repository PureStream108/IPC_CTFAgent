from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from backend.mcp import reverse_mcp
from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import SERVER_NAMES, build_mcp_server
from backend.mcp.reverse_mcp import build_reverse_mcp
from backend.mcp.shared import build_browser_mcp, build_zap_mcp
from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
from backend.tools.tool_registry import ToolRegistry


def call_tool(server, tool: str, **arguments):
    async def run():
        async with MCPClient.in_process(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run())


def tool_names(server):
    async def run():
        async with MCPClient.in_process(server) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(run())


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(cache_db=tmp_path / "tool_cache.db").load()


def test_registry_loads_all_categories(registry):
    cats = registry.categories()
    for category in ("web", "reverse", "crypto", "pwn", "misc", "ai", "osint"):
        assert category in cats
    assert registry.get("sqlmap") is not None


def test_exposed_for_category(registry):
    web = registry.exposed_for("web")
    names = {tool.name for tool in web}
    assert {"browser", "zap", "sqlmap", "typhonbreaker"} <= names
    assert all(tool.category == "web" for tool in web)
    assert all(tool.description and tool.exec and tool.when_to_use for tool in web)
    assert "ghidra" not in names


def test_tool_search_finds_cross_category(registry):
    results = registry.search("rsa lattice factoring")
    names = {tool.name for tool in results}
    assert "rsactftool" in names or "sage" in names


def test_tool_search_finds_pyjail_helper(registry):
    results = registry.search("python pyjail sandbox blacklist builtins")
    assert "typhonbreaker" in {tool.name for tool in results}


def test_tool_search_cache(registry):
    registry.search("memory forensics")
    cached = registry.cached_search("memory forensics")
    assert cached is not None
    assert "volatility3" in cached


def test_tool_search_mcp(registry):
    mcp = build_tool_search_mcp(registry)
    hits = call_tool(mcp, "tool_search", query="ssti flask template")
    assert any(hit["name"] == "fenjing" for hit in hits)


def test_category_tools_mcp(registry):
    mcp = build_category_tools_mcp(registry, "pwn")
    tools = call_tool(mcp, "list_tools")
    assert any(tool["name"] == "pwntools" for tool in tools)
    assert call_tool(mcp, "get_tool", name="gdb")["exec"] == "gdb"
    assert call_tool(mcp, "get_tool", name="nope")["error"]


def test_category_tools_mcp_returns_tool_contract(registry):
    mcp = build_category_tools_mcp(registry, "web")
    listed = call_tool(mcp, "list_tools")
    typhon = next(tool for tool in listed if tool["name"] == "typhonbreaker")
    assert set(typhon) == {"name", "category", "description", "exec", "tags", "when_to_use"}
    detail = call_tool(mcp, "get_tool", name="typhonbreaker")
    assert detail["exec"] == typhon["exec"]
    assert detail["when_to_use"] == typhon["when_to_use"]


def test_removed_webshell_mcp_is_not_exposed():
    removed_name = "".join(("ant", "sword"))
    assert removed_name not in SERVER_NAMES
    with pytest.raises(ValueError, match="unknown MCP server"):
        build_mcp_server(removed_name)


def test_browser_mcp_exposes_stateful_playwright_tools(tmp_path):
    class FakeLocator:
        async def inner_text(self):
            return "Rendered visible text"

    class FakeKeyboard:
        async def press(self, key):
            self.key = key

    class FakeRequest:
        redirected_from = None

    class FakeResponse:
        status = 200
        request = FakeRequest()

        async def all_headers(self):
            return {"content-type": "text/html"}

        async def body(self):
            return b"<html><body>Rendered visible text</body></html>"

    class FakePage:
        url = "about:blank"
        keyboard = FakeKeyboard()

        async def goto(self, url, **kwargs):
            self.url = f"{url}/rendered"
            return FakeResponse()

        async def title(self):
            return "Rendered title"

        def locator(self, selector):
            assert selector == "body"
            return FakeLocator()

        async def click(self, selector):
            self.clicked = selector

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def fill(self, selector, value):
            self.filled = (selector, value)

        async def evaluate(self, script):
            return "Rendered title"

        async def content(self):
            return "<html><body>Rendered visible text</body></html>"

        async def screenshot(self, path, full_page):
            from pathlib import Path

            Path(path).write_bytes(b"png")

    class FakeContext:
        async def cookies(self):
            return [{"name": "session", "value": "abc"}]

        async def add_cookies(self, cookies):
            self.added = cookies

    class FakeBrowserSession:
        _playwright = None

        def __init__(self):
            self._page = FakePage()
            self._context = FakeContext()

        async def page(self):
            return self._page

        async def context(self):
            return self._context

    browser = build_browser_mcp(FakeBrowserSession())

    assert tool_names(browser) == {
        "navigate", "click", "fill", "press", "eval_js", "get_content",
        "screenshot", "cookies", "set_cookie",
    }
    nav = call_tool(browser, "navigate", url="http://challenge")
    assert nav["available"] is True
    assert nav["title"] == "Rendered title"
    assert nav["text"] == "Rendered visible text"
    assert call_tool(browser, "eval_js", script="document.title")["result"] == "Rendered title"
    shot = call_tool(browser, "screenshot", path=str(tmp_path / "page.png"))
    assert shot["available"] is True
    assert (tmp_path / "page.png").read_bytes() == b"png"
    assert call_tool(browser, "cookies")["cookies"][0]["name"] == "session"


def test_reverse_mcp_and_zap_contracts(monkeypatch, tmp_path):
    reverse = build_reverse_mcp()
    assert {
        "decompile", "decompile_all", "list_functions", "strings",
        "disassemble", "r2_cmd", "checksec", "file_info",
    } <= tool_names(reverse)
    missing = call_tool(reverse, "decompile", binary=str(tmp_path / "missing.bin"))
    assert missing["available"] is False
    assert "binary not found" in missing["error"]

    class FakeZapResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"scan": "1", "alerts": [{"risk": "Low"}], "urls": ["http://x/a"]}

    monkeypatch.setattr("backend.mcp.shared.requests.get", lambda url, **kwargs: FakeZapResponse())
    scan = call_tool(build_zap_mcp(), "active_scan", url="http://x")
    assert scan["available"] is True
    assert scan["alerts"] == [{"risk": "Low"}]


def test_reverse_decompile_falls_back_to_r2(monkeypatch, tmp_path):
    binary = tmp_path / "challenge.bin"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(
        reverse_mcp,
        "_open_program",
        lambda path: (_ for _ in ()).throw(RuntimeError("jvm failed")),
    )
    monkeypatch.setattr(
        reverse_mcp,
        "_r2_cmd_sync",
        lambda binary, cmd: {"available": True, "output": "push rbp"},
    )

    result = call_tool(build_reverse_mcp(), "decompile", binary=str(binary), function="main")

    assert result["available"] is True
    assert result["fallback"] == "r2"
    assert result["disassembly"] == "push rbp"


def test_reverse_jvm_uses_bundled_paths_when_stdio_filters_environment(monkeypatch, tmp_path):
    ghidra_dir = tmp_path / "ghidra"
    java_home = tmp_path / "java21"
    ghidra_dir.mkdir()
    java_home.mkdir()
    starts = []
    fake_pyghidra = SimpleNamespace(start=lambda **kwargs: starts.append(kwargs))

    monkeypatch.delitem(sys.modules, "pyghidra", raising=False)
    monkeypatch.setitem(sys.modules, "pyghidra", fake_pyghidra)
    monkeypatch.delenv("GHIDRA_INSTALL_DIR", raising=False)
    monkeypatch.delenv("GHIDRA_JAVA_HOME", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(reverse_mcp, "_DEFAULT_GHIDRA_INSTALL_DIR", ghidra_dir)
    monkeypatch.setattr(reverse_mcp, "_DEFAULT_GHIDRA_JAVA_HOME", java_home)
    monkeypatch.setattr(reverse_mcp, "_JVM_STARTED", False)

    reverse_mcp._ensure_jvm()

    assert starts == [{"verbose": False, "install_dir": ghidra_dir}]
    assert reverse_mcp.os.environ["GHIDRA_JAVA_HOME"] == str(java_home)
    assert reverse_mcp.os.environ["JAVA_HOME"] == str(java_home)
    assert reverse_mcp.os.environ["PATH"].split(reverse_mcp.os.pathsep)[0] == str(java_home / "bin")


def test_server_names_use_reverse_instead_of_ghidra():
    assert "reverse" in SERVER_NAMES
    assert "ghidra" not in SERVER_NAMES
    assert build_mcp_server("reverse").name == "reverse"
