from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from backend.persistence.database import Database, PostgresDatabase


CATEGORIES = ("knowledge", "tool_usage", "exploit", "lessons")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


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
        return {word for word in _tokenize(text) if len(word) >= 2}


def _tokenize(text: str) -> list[str]:
    output: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isalnum() or character == "_":
            current.append(character)
        elif current:
            output.append("".join(current))
            current = []
    if current:
        output.append("".join(current))
    return output


class MemoryStore:
    def __init__(
        self,
        database: PostgresDatabase | str | Path | None = None,
        export_dir: str | Path | None = None,
        in_memory: bool = False,
    ) -> None:
        del in_memory
        if isinstance(database, PostgresDatabase):
            self.db = database
            self._owns_db = False
        else:
            self.db = Database(database)
            self._owns_db = True
        self.export_dir = Path(export_dir) if export_dir else None
        self._configured = False

    def configure(self) -> MemoryStore:
        if self._owns_db:
            self.db.configure()
        self._configured = True
        return self

    def _next_id(self, connection) -> str:
        row = connection.execute(
            "UPDATE mem_counter SET value = value + 1 WHERE name = 'memory' RETURNING value"
        ).fetchone()
        return f"mem_{row['value']:04d}"

    def add(
        self,
        category: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        project_id: str | None = None,
        source: str = "diamond",
        connection=None,
        mirror: bool = True,
    ) -> Memory:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        normalized_tags = list(tags or [])
        now = _utcnow()
        if connection is None:
            with self.db.connect() as own_connection:
                memory_id = self._insert(
                    own_connection,
                    category,
                    title,
                    content,
                    normalized_tags,
                    project_id,
                    source,
                    now,
                )
        else:
            memory_id = self._insert(
                connection,
                category,
                title,
                content,
                normalized_tags,
                project_id,
                source,
                now,
            )
        memory = Memory(
            id=memory_id,
            category=category,
            title=title,
            content=content,
            tags=normalized_tags,
            project_id=project_id,
            source=source,
            created_at=now,
        )
        if mirror:
            self._mirror_to_disk(memory)
        return memory

    def _insert(
        self,
        connection,
        category: str,
        title: str,
        content: str,
        tags: list[str],
        project_id: str | None,
        source: str,
        created_at: str,
    ) -> str:
        memory_id = self._next_id(connection)
        connection.execute(
            """
            INSERT INTO memories (id, category, title, content, tags, project_id, source, created_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                memory_id,
                category,
                title,
                content,
                json.dumps(tags, ensure_ascii=False),
                project_id,
                source,
                created_at,
            ),
        )
        return memory_id

    def get(self, memory_id: str) -> Memory | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = %s", (memory_id,)
            ).fetchone()
        return _row_to_memory(row) if row else None

    def list(self, category: str | None = None) -> list[Memory]:
        with self.db.connect() as connection:
            if category:
                rows = connection.execute(
                    "SELECT * FROM memories WHERE category = %s ORDER BY created_at DESC",
                    (category,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC"
                ).fetchall()
        return [_row_to_memory(row) for row in rows]

    def delete(self, memory_id: str) -> bool:
        with self.db.connect() as connection:
            cursor = connection.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
        return cursor.rowcount > 0

    def all(self) -> list[Memory]:
        return self.list()

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _mirror_to_disk(self, memory: Memory) -> None:
        if self.export_dir is None:
            return
        folder = self.export_dir / memory.category
        folder.mkdir(parents=True, exist_ok=True)
        body = (
            "---\n"
            f"id: {memory.id}\n"
            f"category: {memory.category}\n"
            f"tags: [{', '.join(memory.tags)}]\n"
            f"project: {memory.project_id or ''}\n"
            f"source: {memory.source}\n"
            f"created_at: {memory.created_at}\n"
            "---\n\n"
            f"# {memory.title}\n\n{memory.content}\n"
        )
        (folder / f"{memory.id}.md").write_text(body, encoding="utf-8")


def _row_to_memory(row) -> Memory:
    raw_tags = row["tags"] or []
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags)
        except json.JSONDecodeError:
            raw_tags = [tag for tag in raw_tags.split(",") if tag]
    return Memory(
        id=row["id"],
        category=row["category"],
        title=row["title"],
        content=row["content"],
        tags=list(raw_tags),
        project_id=row["project_id"],
        source=row["source"],
        created_at=row["created_at"],
    )
