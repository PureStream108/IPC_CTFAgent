from __future__ import annotations

"""
Export memory as an Obsidian Vault.

Layout:
    vault/
      knowledge/<id>.md
      tool_usage/<id>.md
      exploit/<id>.md
      lessons/<id>.md
      _index.md          (MOC with [[wiki links]])
Each note carries YAML frontmatter and #tags so Obsidian's graph view works.
"""

from pathlib import Path

from backend.memory.memory_store import CATEGORIES, Memory, MemoryStore


def _note_filename(mem: Memory) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in mem.title).strip()
    safe = safe[:60] or mem.id
    return f"{mem.id}-{safe}"


def _note_body(mem: Memory) -> str:
    tag_line = " ".join(f"#{t.replace(' ', '_')}" for t in mem.tags)
    fm = [
        "---",
        f"id: {mem.id}",
        f"category: {mem.category}",
        f"tags: [{', '.join(mem.tags)}]",
        f"project: {mem.project_id or ''}",
        f"source: {mem.source}",
        f"created_at: {mem.created_at}",
        "---",
        "",
        f"# {mem.title}",
        "",
        mem.content,
        "",
    ]
    if tag_line:
        fm.append(tag_line)
        fm.append("")
    fm.append(f"_category: [[_index#{mem.category}]]_")
    return "\n".join(fm)


def export_obsidian(store: MemoryStore, vault_dir: str | Path) -> Path:
    vault = Path(vault_dir)
    vault.mkdir(parents=True, exist_ok=True)
    index_lines = ["# Memory Vault (MOC)", ""]
    for category in CATEGORIES:
        mems = store.list(category)
        folder = vault / category
        folder.mkdir(parents=True, exist_ok=True)
        index_lines.append(f"## {category}")
        index_lines.append("")
        for mem in mems:
            fname = _note_filename(mem)
            (folder / f"{fname}.md").write_text(_note_body(mem), encoding="utf-8")
            index_lines.append(f"- [[{fname}|{mem.title}]]")
        index_lines.append("")
    (vault / "_index.md").write_text("\n".join(index_lines), encoding="utf-8")
    return vault
