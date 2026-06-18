from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

REGISTRY_DIR = Path(__file__).resolve().parent / "registry"

# Public tools/MCPs every project may use regardless of category.
PUBLIC_MCPS = ("browser", "ghidra", "zap")
# Languages/runtimes always available in the member sandbox.
LANGUAGES = ("python", "java", "go", "rust", "php", "nodejs", "maven")


@dataclass(slots=True)
class Tool:
    name: str
    category: str
    description: str
    path: str
    exec: str
    tags: list[str]
    when_to_use: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "exec": self.exec,
            "tags": self.tags,
            "when_to_use": self.when_to_use,
        }


class ToolRegistry:
    def __init__(self, registry_dir: Path | None = None, cache_db: str | Path | None = None):
        self.registry_dir = registry_dir or REGISTRY_DIR
        self.cache_db = Path(cache_db) if cache_db else None
        self._tools: list[Tool] = []
        self._loaded = False

    def load(self) -> "ToolRegistry":
        if self._loaded:
            return self
        self._tools = []
        for path in sorted(self.registry_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            category = data.get("category", path.stem)
            for entry in data.get("tools", []):
                self._tools.append(
                    Tool(
                        name=entry["name"],
                        category=category,
                        description=entry.get("description", ""),
                        path=entry.get("path", ""),
                        exec=entry.get("exec", ""),
                        tags=list(entry.get("tags", [])),
                        when_to_use=entry.get("when_to_use", ""),
                    )
                )
        if self.cache_db is not None:
            self._init_cache()
        self._loaded = True
        return self

    # ---- category-based exposure ----

    def all_tools(self) -> list[Tool]:
        return list(self._tools)

    def by_category(self, category: str) -> list[Tool]:
        return [t for t in self._tools if t.category == category]

    def exposed_for(self, category: str) -> list[Tool]:
        """Tools initially exposed to a Member of the given project category."""
        return self.by_category(category)

    def get(self, name: str) -> Tool | None:
        for t in self._tools:
            if t.name == name:
                return t
        return None

    def categories(self) -> list[str]:
        return sorted({t.category for t in self._tools})

    # ---- tool_search (cached) ----

    def _init_cache(self) -> None:
        self.cache_db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tool_search_cache ("
                "query TEXT PRIMARY KEY, results TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cache_db), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _score(self, tool: Tool, terms: list[str]) -> float:
        name = tool.name.lower()
        tags = {t.lower() for t in tool.tags}
        text = f"{tool.description} {tool.when_to_use}".lower()
        score = 0.0
        for term in terms:
            if term in name:
                score += 3.0
            if term in tags:
                score += 2.5
            if term in text:
                score += 1.0
        return score

    def search(self, query: str, limit: int = 8) -> list[Tool]:
        terms = [w for w in query.lower().replace(",", " ").split() if len(w) >= 2]
        scored = [(t, self._score(t, terms)) for t in self._tools]
        scored = [(t, s) for t, s in scored if s > 0]
        scored.sort(key=lambda p: p[1], reverse=True)
        results = [t for t, _ in scored[:limit]]
        if self.cache_db is not None and results:
            self._cache_results(query, [t.name for t in results])
        return results

    def _cache_results(self, query: str, names: list[str]) -> None:
        import json
        from datetime import datetime, timezone

        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tool_search_cache (query, results, created_at) VALUES (?, ?, ?)",
                (query, json.dumps(names), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def cached_search(self, query: str) -> list[str] | None:
        if self.cache_db is None:
            return None
        import json

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT results FROM tool_search_cache WHERE query = ?", (query,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["results"]) if row else None
