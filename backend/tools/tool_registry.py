from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Collection

import yaml

REGISTRY_DIR = Path(__file__).resolve().parent / "registry"

# Public tools/MCPs every project may use regardless of category.
PUBLIC_MCPS = ("browser", "reverse", "zap")
# Languages/runtimes always available in the member sandbox.
LANGUAGES = (
    "bash",
    "python",
    "java",
    "go",
    "rust",
    "php",
    "nodejs",
    "ruby",
    "maven",
)


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
    def __init__(
        self,
        registry_dir: Path | None = None,
        cache_db: str | Path | None = None,
        in_memory: bool = False,
    ):
        self.registry_dir = registry_dir or REGISTRY_DIR
        # cache_db/in_memory are accepted for source compatibility only. Search
        # results are a bounded, rebuildable process-local cache.
        del cache_db, in_memory
        self.cache_max_entries = max(1, int(os.environ.get("IPC_TOOL_CACHE_MAX", "512")))
        self.cache_ttl_seconds = max(1, int(os.environ.get("IPC_TOOL_CACHE_TTL", "900")))
        self._cache: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._tools: list[Tool] = []
        self._loaded = False

    def load(self) -> ToolRegistry:
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
        self._loaded = True
        return self

    # ---- category-based exposure ----

    def all_tools(self) -> list[Tool]:
        return list(self._tools)

    def by_category(self, category: str) -> list[Tool]:
        return [t for t in self._tools if t.category == category]

    @staticmethod
    def _available(tool: Tool, available_mcps: Collection[str] | None) -> bool:
        if available_mcps is None or not tool.exec.startswith("mcp:"):
            return True
        return tool.exec.removeprefix("mcp:").strip() in available_mcps

    def exposed_for(
        self,
        category: str,
        *,
        available_mcps: Collection[str] | None = None,
    ) -> list[Tool]:
        """Tools initially exposed to a Member of the given project category."""
        return [
            tool
            for tool in self.by_category(category)
            if self._available(tool, available_mcps)
        ]

    def get(
        self,
        name: str,
        *,
        available_mcps: Collection[str] | None = None,
    ) -> Tool | None:
        for t in self._tools:
            if t.name == name and self._available(t, available_mcps):
                return t
        return None

    def categories(self) -> list[str]:
        return sorted({t.category for t in self._tools})

    # ---- tool_search (cached) ----

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

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        available_mcps: Collection[str] | None = None,
    ) -> list[Tool]:
        terms = [w for w in query.lower().replace(",", " ").split() if len(w) >= 2]
        scored = [
            (tool, self._score(tool, terms))
            for tool in self._tools
            if self._available(tool, available_mcps)
        ]
        scored = [(t, s) for t, s in scored if s > 0]
        scored.sort(key=lambda p: p[1], reverse=True)
        results = [t for t, _ in scored[:limit]]
        if results:
            self._cache_results(query, [t.name for t in results])
        return results

    def _cache_results(self, query: str, names: list[str]) -> None:
        with self._cache_lock:
            self._cache.pop(query, None)
            self._cache[query] = (time.monotonic(), list(names))
            while len(self._cache) > self.cache_max_entries:
                self._cache.popitem(last=False)

    def cached_search(self, query: str) -> list[str] | None:
        with self._cache_lock:
            cached = self._cache.get(query)
            if cached is None:
                return None
            created_at, names = cached
            if time.monotonic() - created_at > self.cache_ttl_seconds:
                self._cache.pop(query, None)
                return None
            self._cache.move_to_end(query)
            return list(names)

    def close(self) -> None:
        with self._cache_lock:
            self._cache.clear()
