from __future__ import annotations

import pytest

from backend.memory.exporter.markdown import export_markdown
from backend.memory.exporter.obsidian import export_obsidian
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_search import search
from backend.memory.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db", export_dir=tmp_path / "mem").configure()


def _seed(store):
    store.add("knowledge", "Flask SSTI", "Server side template injection in Jinja2 via {{7*7}}.", ["web", "ssti", "flask"])
    store.add("tool_usage", "sqlmap basics", "Use sqlmap -u URL --batch for blind SQL injection.", ["web", "sqli", "sqlmap"])
    store.add("exploit", "Shiro 550", "Deserialization RCE with known key, use ysoserial.", ["java", "shiro", "deserialization"])
    store.add("lessons", "Dont brute too early", "Try logic flaws before brute forcing credentials.", ["lessons"])


def test_add_and_list(store):
    _seed(store)
    assert len(store.all()) == 4
    assert len(store.list("knowledge")) == 1
    assert store.list("exploit")[0].title == "Shiro 550"


def test_reject_bad_category(store):
    with pytest.raises(ValueError):
        store.add("bogus", "t", "c")


def test_search_relevance(store):
    _seed(store)
    results = search(store, "flask ssti template")
    assert results
    top = results[0][0]
    assert top.title == "Flask SSTI"


def test_search_category_filter(store):
    _seed(store)
    results = search(store, "sqli sqlmap", category="tool_usage")
    assert len(results) == 1
    assert results[0][0].title == "sqlmap basics"


def test_persistence_across_instances(store, tmp_path):
    _seed(store)
    # New instance pointing at same db file should see the memories.
    store2 = MemoryStore(tmp_path / "memory.db").configure()
    assert len(store2.all()) == 4


def test_disk_mirror_written(store, tmp_path):
    store.add("knowledge", "Title", "Body content", ["t1", "t2"])
    files = list((tmp_path / "mem" / "knowledge").glob("*.md"))
    assert len(files) == 1
    assert "Body content" in files[0].read_text(encoding="utf-8")


def test_memory_mcp_search_and_get(store):
    _seed(store)
    mcp = build_memory_mcp(store)
    hits = mcp.call("memory_search", query="shiro deserialization java")
    assert hits
    assert hits[0]["title"] == "Shiro 550"
    full = mcp.call("memory_get", id=hits[0]["id"])
    assert "ysoserial" in full["content"]
    assert mcp.call("memory_get", id="nope")["error"]


def test_export_markdown(store, tmp_path):
    _seed(store)
    path = export_markdown(store, tmp_path / "export")
    text = path.read_text(encoding="utf-8")
    assert "Flask SSTI" in text
    assert "Tool Usage" in text


def test_export_obsidian_vault(store, tmp_path):
    _seed(store)
    vault = export_obsidian(store, tmp_path / "vault")
    assert (vault / "_index.md").exists()
    notes = list((vault / "knowledge").glob("*.md"))
    assert notes
    body = notes[0].read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "[[_index#knowledge]]" in body


def test_delete(store):
    m = store.add("lessons", "x", "y")
    assert store.delete(m.id) is True
    assert store.get(m.id) is None
