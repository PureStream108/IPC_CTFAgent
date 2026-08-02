from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.mcp import reverse_mcp, reverse_worker
from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import SERVER_NAMES, build_mcp_server
from backend.mcp.reverse_mcp import build_reverse_mcp
from backend.mcp.shared import _BrowserSession, build_browser_mcp, build_zap_mcp
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


def test_unavailable_mcp_tools_are_filtered(registry):
    web = registry.exposed_for("web", available_mcps={"browser", "reverse"})
    assert "browser" in {tool.name for tool in web}
    assert "zap" not in {tool.name for tool in web}
    assert registry.get("zap", available_mcps={"browser", "reverse"}) is None
    assert not registry.search("zap spider", available_mcps={"browser", "reverse"})

    mcp = build_category_tools_mcp(
        registry, "web", available_mcps={"browser", "reverse"}
    )
    assert "zap" not in {tool["name"] for tool in call_tool(mcp, "list_tools")}
    assert call_tool(mcp, "get_tool", name="zap")["error"]

    search_mcp = build_tool_search_mcp(
        registry, available_mcps={"browser", "reverse"}
    )
    assert not call_tool(search_mcp, "tool_search", query="zap spider")


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
        "screenshot", "cookies", "set_cookie", "wait_for", "network_log_start",
        "network_log_list", "network_log_stop", "console_logs", "page_errors",
        "upload_file", "download",
    }
    nav = call_tool(browser, "navigate", url="http://challenge")
    assert nav["available"] is True
    assert nav["title"] == "Rendered title"
    assert nav["text"] == "Rendered visible text"
    assert call_tool(browser, "eval_js", script="document.title")["result"] == "Rendered title"
    shot = call_tool(browser, "screenshot", path=str(tmp_path / "page.png"))
    assert shot["available"] is True
    assert (tmp_path / "page.png").read_bytes() == b"png"
    cookie = call_tool(browser, "cookies")["cookies"][0]
    assert cookie["name"] == "session"
    assert cookie["value"] == "[REDACTED]"
    assert call_tool(browser, "cookies", include_values=True)["cookies"][0]["value"] == "abc"


def test_browser_session_event_buffers_are_bounded_incremental_and_redacted(tmp_path):
    session = _BrowserSession(
        workdir=tmp_path / "member",
        shared_dir=tmp_path / "shared",
        artifact_root=tmp_path / "artifacts",
        event_limit=2,
        console_limit=1,
        error_limit=1,
    )
    session._active_page_id = "main"
    session._record_console(
        "main",
        SimpleNamespace(
            type="error",
            text=(
                "token=console-secret Authorization: Bearer header-secret\n"
                "https://target.test/a?api_key=query-secret"
            ),
            location={"url": "app.js", "lineNumber": 1},
        ),
    )
    session._record_console(
        "main", SimpleNamespace(type="warning", text="safe warning", location={})
    )
    console = session.console_logs(after_id=None, limit=50, levels=None)
    assert console["dropped_count"] == 1
    assert [event["text"] for event in console["events"]] == ["safe warning"]

    session._record_page_error(
        "main", RuntimeError("Authorization: Bearer error-secret")
    )
    errors = session.page_errors(after_id=None, limit=50)
    assert "error-secret" not in errors["events"][0]["message"]

    class FakeRequest:
        method = "POST"
        url = "https://target.test/api?token=request-secret&view=short"
        resource_type = "fetch"
        redirected_from = None

    class FakeResponse:
        status = 200
        request = FakeRequest()

        async def all_headers(self):
            return {"content-type": "application/json; charset=utf-8"}

        async def body(self):
            return b'{"token=body-secret":"visible"}'

    session._network_active = True
    session._network_capture_preview = True
    session._network_preview_limit = 4096
    session._on_request(FakeResponse.request)
    asyncio.run(session._record_response(FakeResponse()))
    network = asyncio.run(session.network_log_list(
        after_id=None, limit=50, url_contains=None, methods=["post"], statuses=[200]
    ))
    assert len(network["events"]) == 1
    event = network["events"][0]
    assert "request-secret" not in event["url"]
    assert "body-secret" not in event["response_preview"]
    assert asyncio.run(session.network_log_list(
        after_id=event["event_id"], limit=50, url_contains=None, methods=None, statuses=None
    ))["events"] == []


def test_browser_session_enforces_boundaries_and_records_artifacts(tmp_path):
    member = tmp_path / "member"
    shared = tmp_path / "shared"
    artifacts = member / "browser-artifacts"
    member.mkdir()
    shared.mkdir()
    upload = member / "payload.txt"
    upload.write_text("payload", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    session = _BrowserSession(
        project_id="proj_001",
        member="aventurine",
        workdir=member,
        shared_dir=shared,
        artifact_root=artifacts,
        allowed_origins=["https://target.test"],
    )

    assert session.validate_url("https://target.test/path") == "https://target.test/path"
    with pytest.raises(ValueError, match="not allowed"):
        session.validate_url("https://other.test/path")
    with pytest.raises(ValueError, match="HTTP"):
        session.validate_url("file:///etc/passwd")

    class FakeRoute:
        def __init__(self):
            self.action = None

        async def abort(self, reason):
            self.action = ("abort", reason)

        async def continue_(self):
            self.action = ("continue", None)

    allowed_route = FakeRoute()
    asyncio.run(
        session._route_request(allowed_route, SimpleNamespace(url="https://target.test/app.js"))
    )
    assert allowed_route.action == ("continue", None)
    blocked_route = FakeRoute()
    asyncio.run(
        session._route_request(blocked_route, SimpleNamespace(url="https://other.test/app.js"))
    )
    assert blocked_route.action == ("abort", "blockedbyclient")
    assert session.validate_upload_paths(["payload.txt"]) == [upload.resolve()]
    with pytest.raises(ValueError, match="outside"):
        session.validate_upload_paths([str(outside)])
    symlink = member / "outside-link.txt"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="outside"):
            session.validate_upload_paths([str(symlink)])

    artifact_path = session.new_artifact_path("screenshots", suffix=".png")
    artifact_path.write_bytes(b"png")
    artifact = session.record_artifact(
        tool="screenshot",
        artifact_type="screenshot",
        path=artifact_path,
        url="https://target.test/page?token=metadata-secret",
    )
    assert artifact["artifact_id"].startswith("screenshot:")
    assert artifact["relative_path"].startswith("screenshots/")
    metadata = json.loads((artifacts / "metadata.jsonl").read_text(encoding="utf-8"))
    assert metadata["project_id"] == "proj_001"
    assert metadata["member"] == "aventurine"
    assert metadata["sha256"] == artifact["sha256"]
    assert "metadata-secret" not in metadata["url"]

    oversized_session = _BrowserSession(
        workdir=member,
        shared_dir=shared,
        artifact_root=member / "small-artifacts",
        artifact_max_bytes=2,
    )
    oversized = oversized_session.new_artifact_path("downloads", suffix=".bin")
    oversized.write_bytes(b"too large")
    with pytest.raises(ValueError, match="per-file limit"):
        oversized_session.record_artifact(
            tool="download", artifact_type="download", path=oversized, url="https://target.test"
        )
    assert not oversized.exists()


def test_browser_phase_one_tools_wait_upload_download_and_artifacts(tmp_path):
    member = tmp_path / "member"
    shared = tmp_path / "shared"
    member.mkdir()
    shared.mkdir()
    upload = member / "payload.bin"
    upload.write_bytes(b"upload")

    class FakeDownload:
        suggested_filename = "report.txt"

        async def save_as(self, path):
            Path(path).write_bytes(b"download")

    class FakeDownloadInfo:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @property
        def value(self):
            async def resolve():
                return FakeDownload()

            return resolve()

    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector

        async def inner_text(self):
            return "ready"

        async def set_input_files(self, paths):
            self.paths = paths

        async def click(self):
            return None

    class FakePage:
        url = "https://target.test/page"

        def locator(self, selector):
            return FakeLocator(selector)

        async def title(self):
            return "Ready"

        async def wait_for_selector(self, selector, **kwargs):
            self.waited_selector = selector

        async def wait_for_url(self, url, **kwargs):
            self.waited_url = url

        async def wait_for_load_state(self, state, **kwargs):
            self.waited_state = state

        async def screenshot(self, path, full_page):
            Path(path).write_bytes(b"png")

        def expect_download(self, **kwargs):
            return FakeDownloadInfo()

    class FakeContext:
        async def cookies(self):
            return []

    class FakeSession(_BrowserSession):
        def __init__(self):
            super().__init__(
                project_id="proj_001",
                member="aventurine",
                workdir=member,
                shared_dir=shared,
                artifact_root=member / "browser-artifacts",
                allowed_origins=["https://target.test"],
            )
            self.fake_page = FakePage()
            self.fake_context = FakeContext()

        async def page(self, page_id=None):
            if page_id not in (None, "main"):
                raise ValueError(f"unknown page_id: {page_id}")
            return self.fake_page

        def page_id(self, page=None):
            return "main"

        async def context(self):
            return self.fake_context

    server = build_browser_mcp(FakeSession())
    waited = call_tool(
        server,
        "wait_for",
        selector="#ready",
        url="**/page",
        load_state="domcontentloaded",
    )
    assert waited["available"] is True
    assert waited["page_id"] == "main"
    uploaded = call_tool(server, "upload_file", selector="input[type=file]", paths=["payload.bin"])
    assert uploaded["files"] == [{"name": "payload.bin", "size": 6}]
    screenshot = call_tool(server, "screenshot")
    assert screenshot["artifact_id"].startswith("screenshot:")
    downloaded = call_tool(server, "download", selector="#download")
    assert downloaded["artifact_id"].startswith("download:")
    assert downloaded["suggested_filename"] == "report.txt"
    assert len((member / "browser-artifacts" / "metadata.jsonl").read_text(encoding="utf-8").splitlines()) == 2


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
        "_run_ghidra_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("jvm failed")),
    )
    monkeypatch.setattr(
        reverse_mcp,
        "_r2_decompile_sync",
        lambda binary, function: {"available": True, "output": "push rbp"},
    )

    result = call_tool(build_reverse_mcp(), "decompile", binary=str(binary), function="main")

    assert result["available"] is True
    assert result["fallback"] == "r2"
    assert result["disassembly"] == "push rbp"


def test_reverse_r2_fallback_resolves_symbol_to_numeric_address(monkeypatch, tmp_path):
    binary = tmp_path / "challenge.bin"
    binary.write_bytes(b"binary")
    commands = []

    class FakeR2:
        def cmd(self, command):
            commands.append(command)
            if command.startswith("pdf @"):
                return "push rbp\nret"
            return ""

        def cmdj(self, command):
            assert command == "aflj"
            return [{"name": "sym.secret_check", "offset": 0x401126}]

        def quit(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "r2pipe",
        SimpleNamespace(open=lambda *args, **kwargs: FakeR2()),
    )

    for selector in ("secret_check", "sym.secret_check", "0x401126"):
        commands.clear()
        result = reverse_mcp._r2_decompile_sync(str(binary), selector)

        assert result["available"] is True
        assert result["address"] == 0x401126
        assert commands == ["aaa", f"pdf @ {0x401126}"]
        assert selector not in commands[-1]


def test_reverse_r2_fallback_rejects_empty_output(monkeypatch, tmp_path):
    binary = tmp_path / "challenge.bin"
    binary.write_bytes(b"binary")

    class FakeR2:
        def cmd(self, command):
            return ""

        def cmdj(self, command):
            return [{"name": "sym.main", "offset": 0x401000}]

        def quit(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "r2pipe",
        SimpleNamespace(open=lambda *args, **kwargs: FakeR2()),
    )

    result = reverse_mcp._r2_decompile_sync(str(binary), "main")

    assert result["available"] is False
    assert "empty disassembly" in result["error"]


def test_reverse_worker_hard_timeout_cleans_temporary_project(monkeypatch, tmp_path):
    binary = tmp_path / "sample"
    binary.write_bytes(b"\x7fELF")
    observed: dict[str, Path] = {}

    def hang(command, **kwargs):
        project_dir = Path(command[command.index("--project-dir") + 1])
        observed["project"] = project_dir
        assert project_dir.is_dir()
        assert kwargs["timeout"] == 2
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(reverse_mcp.subprocess, "run", hang)

    with pytest.raises(TimeoutError, match="hard timeout"):
        reverse_mcp._run_ghidra_worker(
            "decompile", str(binary), 2, function="main"
        )

    assert not observed["project"].exists()


def test_reverse_worker_uses_manual_analysis_and_closes_context(
    monkeypatch, tmp_path
):
    binary = tmp_path / "sample"
    binary.write_bytes(b"\x7fELF")
    events = []

    class Manager:
        def getFunctions(self, forward):
            assert forward is True
            return []

    class Program:
        def getFunctionManager(self):
            return Manager()

    class Flat:
        def getCurrentProgram(self):
            return Program()

    class Context:
        def __enter__(self):
            events.append("enter")
            return Flat()

        def __exit__(self, *args):
            events.append("exit")

    def open_program(path, **kwargs):
        events.append(("open", path, kwargs))
        return Context()

    fake_pyghidra = SimpleNamespace(
        open_program=open_program,
        task_monitor=lambda timeout: ("monitor", timeout),
        analyze=lambda program, monitor: events.append(("analyze", monitor)),
    )
    monkeypatch.setitem(sys.modules, "pyghidra", fake_pyghidra)
    monkeypatch.setattr(reverse_mcp, "_ensure_jvm", lambda: events.append("jvm"))

    result = reverse_worker._run(
        "list_functions",
        str(binary),
        10,
        project_dir=str(tmp_path / "project"),
    )

    assert result["available"] is True
    assert result["functions"] == []
    assert events[0] == "jvm"
    assert events[1][0] == "open"
    assert events[1][2]["analyze"] is False
    assert any(
        isinstance(event, tuple) and event[0] == "analyze" for event in events
    )
    assert events[-1] == "exit"


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
