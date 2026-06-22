from __future__ import annotations

import pytest

from backend.mcp.antsword import build_antsword_mcp
from backend.mcp import shared as shared_mcp
from backend.mcp.shared import build_browser_mcp, build_ghidra_mcp, build_zap_mcp
from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
from backend.tools.tool_registry import ToolRegistry


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(cache_db=tmp_path / "tool_cache.db").load()


def test_registry_loads_all_categories(registry):
    cats = registry.categories()
    for c in ("web", "reverse", "crypto", "pwn", "misc", "ai", "osint"):
        assert c in cats
    assert registry.get("sqlmap") is not None


def test_exposed_for_category(registry):
    web = registry.exposed_for("web")
    names = {t.name for t in web}
    assert "sqlmap" in names
    assert "typhonbreaker" in names
    assert all(t.category == "web" for t in web)
    assert all(t.description and t.exec and t.when_to_use for t in web)
    typhon = registry.get("typhonbreaker")
    assert typhon is not None
    assert typhon.category == "web"
    assert typhon.tags
    assert "ghidra" not in names  # reverse-only


def test_tool_search_finds_cross_category(registry):
    results = registry.search("rsa lattice factoring")
    names = {t.name for t in results}
    assert "rsactftool" in names or "sage" in names


def test_tool_search_finds_pyjail_helper(registry):
    results = registry.search("python pyjail sandbox blacklist builtins")
    names = {t.name for t in results}
    assert "typhonbreaker" in names


def test_tool_search_cache(registry):
    registry.search("memory forensics")
    cached = registry.cached_search("memory forensics")
    assert cached is not None
    assert "volatility3" in cached


def test_tool_search_mcp(registry):
    mcp = build_tool_search_mcp(registry)
    hits = mcp.call("tool_search", query="ssti flask template")
    assert any(h["name"] == "fenjing" for h in hits)


def test_category_tools_mcp(registry):
    mcp = build_category_tools_mcp(registry, "pwn")
    tools = mcp.call("list_tools")
    assert any(t["name"] == "pwntools" for t in tools)
    got = mcp.call("get_tool", name="gdb")
    assert got["exec"] == "gdb"
    assert mcp.call("get_tool", name="nope")["error"]


def test_category_tools_mcp_returns_tool_contract(registry):
    mcp = build_category_tools_mcp(registry, "web")
    listed = mcp.call("list_tools")
    typhon = next(t for t in listed if t["name"] == "typhonbreaker")
    assert set(typhon) == {"name", "category", "description", "exec", "tags", "when_to_use"}
    assert typhon["category"] == "web"
    assert typhon["exec"]
    assert typhon["tags"]

    detail = mcp.call("get_tool", name="typhonbreaker")
    assert detail["name"] == typhon["name"]
    assert detail["exec"] == typhon["exec"]
    assert detail["when_to_use"] == typhon["when_to_use"]


def test_antsword_encoder():
    mcp = build_antsword_mcp()
    out = mcp.call("encoder", data="system('id')", scheme="base64")
    import base64
    assert base64.b64decode(out["encoded"]).decode() == "system('id')"


def test_antsword_webshell_and_upload():
    mcp = build_antsword_mcp()
    shell = mcp.call("webshell_generator", kind="php", password="pw")
    assert shell["safe_stub"] is True
    assert "SAFE_TEMPLATE[php]" in shell["shell"]
    assert shell["password"] == "pw"
    up = mcp.call("upload", content=shell["shell"], filename="x.php")
    assert "multipart/form-data" in up["headers"]["Content-Type"]
    assert "x.php" in up["body"]


def test_antsword_php_bypass_and_mutation():
    mcp = build_antsword_mcp()
    bp = mcp.call("php_bypass", technique="disable_functions_FFI")
    assert "FFI" in bp["snippet"]
    assert "omitted by the safe stub" in bp["snippet"]
    mut = mcp.call("traffic_mutation", payload="whoami", method="comment_insert")
    assert "/**/" in mut["mutated"] or mut["mutated"] == "whoami"


def test_antsword_has_five_tools():
    mcp = build_antsword_mcp()
    names = {t["name"] for t in mcp.list_tools()}
    assert names == {"encoder", "upload", "php_bypass", "traffic_mutation", "webshell_generator"}


def test_shared_mcps(monkeypatch, tmp_path):
    class FakeResponse:
        url = "http://x/final"
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><title>Hello</title><body>Visible text</body></html>"

        def raise_for_status(self):
            return None

        def json(self):
            return {"scan": "1", "alerts": [{"risk": "Low"}], "urls": ["http://x/a"]}

    def fake_get(url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("backend.mcp.shared.requests.get", fake_get)

    b = build_browser_mcp()
    nav = b.call("navigate", url="http://x")
    assert nav["available"] is True
    assert nav["status"] == 200
    assert nav["title"] == "Hello"
    g = build_ghidra_mcp()
    missing = g.call("decompile", binary=str(tmp_path / "missing.bin"))
    assert missing["available"] is False
    assert "stub" not in missing["error"].lower()
    z = build_zap_mcp()
    scan = z.call("active_scan", url="http://x")
    assert scan["available"] is True
    assert scan["alerts"] == [{"risk": "Low"}]


def test_shared_mcps_use_configured_or_bundled_docker_paths(monkeypatch, tmp_path):
    chrome = tmp_path / "chromium"
    ghidra = tmp_path / "analyzeHeadless"
    nm = tmp_path / "nm"
    for path in (chrome, ghidra, nm):
        path.write_text("", encoding="utf-8")

    monkeypatch.setenv("IPC_CHROME_BIN", str(chrome))
    monkeypatch.setenv("IPC_GHIDRA_HEADLESS", str(ghidra))
    monkeypatch.setenv("IPC_NM_BIN", str(nm))

    assert shared_mcp._chrome_bin() == str(chrome)
    assert shared_mcp._ghidra_headless() == str(ghidra)
    assert shared_mcp._configured_or_bundled("IPC_NM_BIN", ()) == str(nm)


def test_shared_mcps_do_not_search_host_path(monkeypatch, tmp_path):
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    (host_bin / "chromium").write_text("", encoding="utf-8")
    (host_bin / "analyzeHeadless").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(host_bin))
    monkeypatch.delenv("IPC_CHROME_BIN", raising=False)
    monkeypatch.delenv("IPC_GHIDRA_HEADLESS", raising=False)
    monkeypatch.setattr(shared_mcp, "_BUNDLED_CHROME_PATHS", ())
    monkeypatch.setattr(shared_mcp, "_BUNDLED_GHIDRA_HEADLESS_PATHS", ())

    assert shared_mcp._chrome_bin() is None
    assert shared_mcp._ghidra_headless() is None
