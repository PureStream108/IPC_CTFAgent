from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from pydantic import BaseModel, Field

CATEGORIES = ("knowledge", "tool_usage", "exploit", "lessons")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    source TEXT NOT NULL DEFAULT 'diamond',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mem_counter (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO mem_counter (name, value) VALUES ('memory', 0);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Memory(BaseModel):
    id: str
    category: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    project_id: str | None = None
    source: str = "diamond"
    created_at: str

    def keywords(self) -> set[str]:
        text = f"{self.title} {self.content} {' '.join(self.tags)}".lower()
        return {w for w in _tokenize(text) if len(w) >= 2}


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    cur = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


class MemoryStore:
    def __init__(self, db_path: str | Path, export_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.export_dir = Path(export_dir) if export_dir else None
        self._lock = threading.Lock()
        self._configured = False

    def configure(self) -> "MemoryStore":
        with self._lock:
            if self._configured:
                return self
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
            self._configured = True
        return self

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _next_id(self, conn: sqlite3.Connection) -> str:
        conn.execute("UPDATE mem_counter SET value = value + 1 WHERE name = 'memory'")
        row = conn.execute("SELECT value FROM mem_counter WHERE name = 'memory'").fetchone()
        return f"mem_{row['value']:04d}"

    def add(
        self,
        category: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        project_id: str | None = None,
        source: str = "diamond",
    ) -> Memory:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        tags = tags or []
        now = _utcnow()
        with self._connect() as conn:
            mid = self._next_id(conn)
            conn.execute(
                "INSERT INTO memories (id, category, title, content, tags, project_id, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, category, title, content, ",".join(tags), project_id, source, now),
            )
        mem = Memory(
            id=mid, category=category, title=title, content=content, tags=tags,
            project_id=project_id, source=source, created_at=now,
        )
        self._mirror_to_disk(mem)
        return mem

    def get(self, memory_id: str) -> Memory | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_memory(row) if row else None

    def list(self, category: str | None = None) -> list[Memory]:
        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE category = ? ORDER BY created_at DESC", (category,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM memories ORDER BY created_at DESC").fetchall()
        return [_row_to_memory(r) for r in rows]

    def delete(self, memory_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def all(self) -> list[Memory]:
        return self.list()

    def _mirror_to_disk(self, mem: Memory) -> None:
        """Write a markdown copy so memory persists even without the db file."""
        if self.export_dir is None:
            return
        folder = self.export_dir / mem.category
        folder.mkdir(parents=True, exist_ok=True)
        body = (
            f"---\n"
            f"id: {mem.id}\n"
            f"category: {mem.category}\n"
            f"tags: [{', '.join(mem.tags)}]\n"
            f"project: {mem.project_id or ''}\n"
            f"source: {mem.source}\n"
            f"created_at: {mem.created_at}\n"
            f"---\n\n"
            f"# {mem.title}\n\n{mem.content}\n"
        )
        (folder / f"{mem.id}.md").write_text(body, encoding="utf-8")


def _row_to_memory(row: sqlite3.Row) -> Memory:
    tags = [t for t in (row["tags"] or "").split(",") if t]
    return Memory(
        id=row["id"],
        category=row["category"],
        title=row["title"],
        content=row["content"],
        tags=tags,
        project_id=row["project_id"],
        source=row["source"],
        created_at=row["created_at"],
    )
