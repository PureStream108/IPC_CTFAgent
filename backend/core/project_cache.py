from __future__ import annotations

"""Small bounded caches for read paths that must survive brief DB outages.

The API keeps the last successful payload for a few read endpoints so an
OperationalError degrades to slightly stale data instead of a 500.  Those
maps used to be plain dicts that only ever grew: every project ever opened
stayed referenced for the lifetime of the process.  ``BoundedCache`` keeps
the same behaviour with a hard size limit and explicit invalidation.

Use it through ``project_detail_cache`` / ``project_list_cache`` so that
DELETE and archive paths invalidate by project id.
"""

import os
import threading
from collections import OrderedDict
from typing import Generic, TypeVar

DEFAULT_MAX_ENTRIES = 64
MAX_ENTRIES_ENV = "IPC_PROJECT_CACHE_MAX"

T = TypeVar("T")


def configured_limit(default: int = DEFAULT_MAX_ENTRIES) -> int:
    """Read ``IPC_PROJECT_CACHE_MAX``; 0 disables caching, bad values fall back."""

    raw = os.environ.get(MAX_ENTRIES_ENV, "").strip()
    if not raw:
        return max(0, int(default))
    try:
        value = int(raw)
    except ValueError:
        return max(0, int(default))
    if value < 0:
        return max(0, int(default))
    return value


class BoundedCache(Generic[T]):
    """Thread-safe LRU map with a hard entry limit.

    ``limit == 0`` disables storage entirely, which keeps the fallback paths
    honest in tests and gives operators an off switch.
    """

    def __init__(self, limit: int = DEFAULT_MAX_ENTRIES, name: str = "cache") -> None:
        self.limit = max(0, int(limit))
        self.name = name
        self._items: OrderedDict[str, T] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> T | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self._hits += 1
                return self._items[key]
            self._misses += 1
            return None

    def put(self, key: str, value: T) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
            self._items[key] = value
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
                self._evictions += 1

    def pop(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._items)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "name": self.name,
                "limit": self.limit,
                "size": len(self._items),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }


project_detail_cache: BoundedCache = BoundedCache(configured_limit(), "project_detail")
project_list_cache: BoundedCache = BoundedCache(1, "project_list")


def invalidate_project(project_id: str) -> None:
    """Drop every cached view of one project (detail plus the list snapshot)."""

    project_detail_cache.pop(project_id)
    project_list_cache.clear()


def reset_caches() -> None:
    """Test helper: empty both caches and restore the configured limits."""

    project_detail_cache.limit = configured_limit()
    project_detail_cache.clear()
    project_list_cache.limit = 1
    project_list_cache.clear()
