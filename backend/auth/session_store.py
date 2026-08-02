from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ActiveSessionStore:
    """Transactional store containing only hashes of active session IDs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def register(self, session_id: str, expires_at: int, now: int) -> None:
        session_hash = self._session_hash(session_id)
        with self._connection() as connection, connection:
            self._delete_expired(connection, now)
            connection.execute(
                """
                INSERT OR REPLACE INTO active_sessions (session_hash, expires_at)
                VALUES (?, ?)
                """,
                (session_hash, expires_at),
            )

    def is_active(self, session_id: str, expires_at: int, now: int) -> bool:
        session_hash = self._session_hash(session_id)
        with self._connection() as connection, connection:
            self._delete_expired(connection, now)
            row = connection.execute(
                """
                SELECT 1
                FROM active_sessions
                WHERE session_hash = ? AND expires_at = ? AND expires_at > ?
                """,
                (session_hash, expires_at, now),
            ).fetchone()
        return row is not None

    def revoke(self, session_id: str, now: int) -> bool:
        session_hash = self._session_hash(session_id)
        with self._connection() as connection, connection:
            self._delete_expired(connection, now)
            cursor = connection.execute(
                "DELETE FROM active_sessions WHERE session_hash = ?",
                (session_hash,),
            )
        return cursor.rowcount > 0

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._prepare_file()
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS active_sessions (
                    session_hash BLOB PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )
            yield connection
        finally:
            connection.close()

    def _prepare_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _delete_expired(connection: sqlite3.Connection, now: int) -> None:
        connection.execute("DELETE FROM active_sessions WHERE expires_at <= ?", (now,))

    @staticmethod
    def _session_hash(session_id: str) -> bytes:
        return hashlib.sha256(session_id.encode("ascii")).digest()
