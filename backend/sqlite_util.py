from __future__ import annotations

"""Shared plumbing for the in-RAM SQLite mode used by the running app.

Operational state (blackboard graph, memory, tool-search cache) is held in RAM
so nothing survives container deletion; only the export/"Derive" actions write
to the persistent ``/data`` mount. The storage classes keep their file-backed
behaviour by default and opt in with ``in_memory=True``.
"""

import sqlite3
import threading
import uuid
from contextlib import nullcontext
from functools import lru_cache
from typing import ContextManager

MEMDB_MIN_VERSION = (3, 36, 0)


@lru_cache(maxsize=1)
def _memdb_available() -> bool:
    """Whether this SQLite build offers the ``memdb`` VFS."""
    if sqlite3.sqlite_version_info < MEMDB_MIN_VERSION:
        return False
    try:
        probe = sqlite3.connect("file:/ipc_memdb_probe?vfs=memdb", uri=True)
    except sqlite3.Error:
        return False
    probe.close()
    return True


class RamSqlite:
    """One private in-RAM database that short-lived connections can reopen.

    Two mechanisms let several connections reach the same RAM database. The
    ``memdb`` VFS (SQLite >= 3.36) keeps ordinary file-style locking, so the
    busy timeout applies and threads simply wait for each other. Legacy
    shared-cache instead raises ``SQLITE_LOCKED`` ("database table is locked")
    the moment two connections touch one table, and the busy timeout does *not*
    cover that. So prefer ``memdb``, and where it is missing fall back to
    shared-cache with every connection serialised behind a lock.
    """

    def __init__(self, name: str):
        token = f"ipc_{name}_{uuid.uuid4().hex}"
        if _memdb_available():
            self.dsn = f"file:/{token}?vfs=memdb"
            self._guard: threading.RLock | None = None
        else:
            self.dsn = f"file:{token}?mode=memory&cache=shared"
            self._guard = threading.RLock()
        # A RAM database is discarded once its last connection closes, so hold
        # one open for as long as this object lives.
        self._keeper = sqlite3.connect(self.dsn, uri=True, check_same_thread=False)
        self._state_lock = threading.RLock()
        self._closed = False

    def guard(self) -> ContextManager[object]:
        """Serialise access when the shared-cache fallback is in use."""
        return self._guard if self._guard is not None else nullcontext()

    def connect(self, timeout: float = 30) -> sqlite3.Connection:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("in-memory SQLite database is closed")
            return sqlite3.connect(
                self.dsn,
                timeout=timeout,
                uri=True,
                check_same_thread=False,
            )

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def close(self) -> None:
        """Idempotently release the keeper connection for this RAM database."""
        with self.guard():
            with self._state_lock:
                if self._closed:
                    return
                self._keeper.close()
                self._closed = True
