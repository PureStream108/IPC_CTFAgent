from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import SERVER_NAMES
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.server.app import create_app
from backend.tools.catalog import CATALOG_DOCS_PATH, CatalogEntry, ToolCatalog
from backend.tools.tool_registry import LANGUAGES, ToolRegistry
from tests.helpers import setup_test_auth, write_mock_config


def _call_tool(server, name: str, **arguments):
    async def run():
        async with MCPClient.in_process(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_catalog_covers_registry_mcps_languages_and_direct_libraries():
    registry = ToolRegistry(cache_db=None).load()
    catalog = ToolCatalog.load(registry)
    entries = catalog.entries()
    titles = {entry.title.casefold() for entry in entries}
    ids = {entry.id for entry in entries}

    assert {tool.name.casefold() for tool in registry.all_tools()} <= titles
    for tool in registry.all_tools():
        entry = next(
            item for item in entries if item.title.casefold() == tool.name.casefold()
        )
        assert f"tools/{tool.category}" in entry.parent_paths()
    assert {f"mcp_{name}" for name in SERVER_NAMES} <= ids
    assert {
        "core_bash",
        "lang_python",
        "lang_java",
        "lang_go",
        "lang_rust",
        "lang_php",
        "lang_nodejs",
        "lang_ruby",
    } <= ids
    assert set(LANGUAGES) >= {
        "bash",
        "python",
        "java",
        "go",
        "rust",
        "php",
        "nodejs",
        "ruby",
        "maven",
    }
    assert {
        "lib_pwntools",
        "lib_pycryptodome",
        "lib_z3",
        "lib_angr",
        "lib_pyghidra",
        "lib_playwright",
        "lib_torch",
    } <= ids


def test_every_catalog_entry_has_one_valid_complete_markdown_document():
    catalog = ToolCatalog.load()
    required = {
        "## 用途与适用场景",
        "## 版本检查",
        "## 命令、导入与镜像路径",
        "## 常用工作流",
        "## 可执行示例",
        "## 输出解释",
        "## 常见错误与限制",
        "## 关联条目",
        "## 官方参考",
    }
    filenames = set()
    ids = {entry.id for entry in catalog.entries()}
    for entry in catalog.entries():
        path = CATALOG_DOCS_PATH / f"{entry.id}.md"
        assert path.is_file()
        assert path.name not in filenames
        filenames.add(path.name)
        markdown = catalog.document(entry.id)
        assert markdown.startswith(f"# {entry.title}\n")
        assert required <= set(markdown.splitlines())
        linked = set(
            re.findall(r"/memory/catalog/([a-z0-9][a-z0-9_-]*)/document", markdown)
        )
        assert linked <= ids


def test_catalog_validation_rejects_duplicate_missing_and_broken_documents(tmp_path):
    entry = CatalogEntry(
        id="sample",
        kind="tool",
        group="tools/core",
        title="Sample",
        summary="One line.",
    )
    with pytest.raises(ValueError, match="duplicate"):
        ToolCatalog([entry, entry], {}, None)
    with pytest.raises(ValueError, match="missing catalog document"):
        ToolCatalog([entry], {}, tmp_path)

    (tmp_path / "sample.md").write_text(
        "[broken](/memory/catalog/not_present/document)",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown id"):
        ToolCatalog([entry], {}, tmp_path)


def test_catalog_mcp_search_browse_and_read(tmp_path):
    from backend.persistence.database import Database

    store = MemoryStore(Database().configure()).configure()
    server = build_memory_mcp(store, catalog=ToolCatalog.load())
    try:
        for query, expected in (
            ("sqlmap", "tool_sqlmap"),
            ("PyGhidra", "lib_pyghidra"),
            ("Java", "lang_java"),
        ):
            hits = _call_tool(server, "memory_search", query=query, limit=10)
            assert any(hit["doc_id"] == expected for hit in hits)
            assert all(
                {"entry_type", "doc_id", "doc_url"} <= set(hit) for hit in hits
            )

        root = _call_tool(server, "memory_catalog")
        assert {node["path"] for node in root["children"]} == {
            "languages",
            "libraries",
            "mcps",
            "tools",
        }
        document = _call_tool(server, "memory_doc", id="lib_pyghidra")
        assert document["doc_url"].endswith("/lib_pyghidra/document")
        assert "## 可执行示例" in document["markdown"]
        assert _call_tool(server, "memory_doc", id="../catalog.yaml")["error"]
    finally:
        store.close()


def test_catalog_web_api_and_path_traversal(tmp_path, monkeypatch):
    write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as client:
        setup_test_auth(client)
        tree = client.get("/memory/catalog")
        assert tree.status_code == 200
        assert len(tree.json()["children"]) == 4

        detail = client.get("/memory/catalog/lib_pyghidra")
        assert detail.status_code == 200
        assert "<h1>PyGhidra</h1>" in detail.json()["html"]
        assert "## 官方参考" in detail.json()["markdown"]

        raw = client.get("/memory/catalog/lib_pyghidra/document")
        assert raw.status_code == 200
        assert raw.headers["content-type"].startswith("text/markdown")
        assert 'filename="lib_pyghidra.md"' in raw.headers["content-disposition"]
        assert client.get("/memory/catalog/not_found").status_code == 404
        assert client.get("/memory/catalog/%2E%2E%2Fcatalog.yaml").status_code in {
            404,
            307,
        }
