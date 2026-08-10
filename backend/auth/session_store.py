from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from backend.persistence.database import Database, PostgresDatabase


class ActiveSessionStore:
    """PostgreSQL store containing only hashes of active browser session IDs."""

    def __init__(
        self,
        legacy_path: str | Path | None = None,
        database: PostgresDatabase | None = None,
    ) -> None:
        del legacy_path
        self.db = database or Database()
        self._owns_db = database is None
        self._configured = False
        self._lock = threading.Lock()

    def _ensure_configured(self) -> None:
        if self._configured:
            return
        with self._lock:
            if self._configured:
                return
            if self._owns_db:
                self.db.configure()
            self._configured = True

    def register(self, session_id: str, expires_at: int, now: int) -> None:
        self._ensure_configured()
        session_hash = self._session_hash(session_id)
        with self.db.connect() as connection:
            self._delete_expired(connection, now)
            connection.execute(
                """
                INSERT INTO active_sessions (session_hash, expires_at)
                VALUES (%s, %s)
                ON CONFLICT (session_hash) DO UPDATE SET expires_at = EXCLUDED.expires_at
                """,
                (session_hash, expires_at),
            )

    def is_active(self, session_id: str, expires_at: int, now: int) -> bool:
        self._ensure_configured()
        session_hash = self._session_hash(session_id)
        with self.db.connect() as connection:
            self._delete_expired(connection, now)
            row = connection.execute(
                """
                SELECT 1 FROM active_sessions
                WHERE session_hash = %s AND expires_at = %s AND expires_at > %s
                """,
                (session_hash, expires_at, now),
            ).fetchone()
        return row is not None

    def revoke(self, session_id: str, now: int) -> bool:
        self._ensure_configured()
        session_hash = self._session_hash(session_id)
        with self.db.connect() as connection:
            self._delete_expired(connection, now)
            cursor = connection.execute(
                "DELETE FROM active_sessions WHERE session_hash = %s", (session_hash,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _delete_expired(connection, now: int) -> None:
        connection.execute("DELETE FROM active_sessions WHERE expires_at <= %s", (now,))

    @staticmethod
    def _session_hash(session_id: str) -> bytes:
        return hashlib.sha256(session_id.encode("ascii")).digest()

    def close(self) -> None:
        if self._owns_db:
            self.db.close()
