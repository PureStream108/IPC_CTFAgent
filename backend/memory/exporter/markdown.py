from __future__ import annotations

from pathlib import Path

from backend.memory.memory_store import CATEGORIES, MemoryStore


def export_markdown(store: MemoryStore, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# IPC CTF Memory Export", ""]
    for category in CATEGORIES:
        mems = store.list(category)
        if not mems:
            continue
        lines.append(f"## {category.replace('_', ' ').title()}")
        lines.append("")
        for m in mems:
            tags = f"  _tags: {', '.join(m.tags)}_" if m.tags else ""
            lines.append(f"### {m.title}{tags}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
    path = out / "memory.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
