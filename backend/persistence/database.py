from __future__ import annotations

import os
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from backend.persistence.schema import SCHEMA_CONTRACT_STATEMENTS


def database_url(value: str | Path | None = None) -> str:
    """Resolve the single PostgreSQL DSN used by every IPC state store.

    Path-shaped arguments are accepted only for source compatibility with old
    constructors; they never create or open a local database file.
    """

    configured = value.strip() if isinstance(value, str) else ""
    if not configured.startswith(
        ("postgresql://", "postgres://", "postgresql+psycopg://")
    ):
        configured = os.environ.get("IPC_DATABASE_URL", "").strip()
    if configured.startswith("postgresql+psycopg://"):
        # ``+psycopg`` is SQLAlchemy's dialect marker, not valid libpq
        # connection-info syntax.  Runtime callers use psycopg directly.
        return "postgresql://" + configured.removeprefix("postgresql+psycopg://")
    if configured.startswith(("postgresql://", "postgres://")):
        return configured
    return "postgresql://ipc:ipc@127.0.0.1:5432/ipc"


def sqlalchemy_database_url(value: str | Path | None = None) -> str:
    """Return a SQLAlchemy URL that explicitly selects psycopg 3."""

    url = database_url(value)
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


def _normalize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _normalize_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: _normalize(value) for key, value in row.items()}


class Cursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return _normalize_row(self._cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        return [_normalize_row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield _normalize_row(row)


class DatabaseConnection:
    def __init__(self, connection: Connection) -> None:
        self.raw = connection

    def execute(self, statement: str, params: Iterable[Any] | None = None) -> Cursor:
        cursor = self.raw.execute(statement, tuple(params or ()))
        return Cursor(cursor)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


class PostgresDatabase:
    """Synchronous psycopg pool shared by FastAPI routes and worker threads."""

    def __init__(
        self,
        dsn: str | Path | None = None,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
        in_memory: bool | None = None,
    ) -> None:
        del in_memory  # kept only to fail closed without reopening a SQLite path
        self.dsn = database_url(dsn)
        self.min_size = min_size or int(os.environ.get("IPC_DB_POOL_MIN", "2"))
        self.max_size = max_size or int(os.environ.get("IPC_DB_POOL_MAX", "16"))
        self.statement_timeout_ms = statement_timeout_ms or int(
            os.environ.get("IPC_DB_STATEMENT_TIMEOUT_MS", "30000")
        )
        self.lock_timeout_ms = lock_timeout_ms or int(
            os.environ.get("IPC_DB_LOCK_TIMEOUT_MS", "5000")
        )
        self._pool = ConnectionPool(
            conninfo=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            open=False,
            kwargs={"row_factory": dict_row},
            configure=self._configure_connection,
            check=ConnectionPool.check_connection,
        )
        self._opened = False

    def _configure_connection(self, connection: Connection) -> None:
        connection.execute("SET timezone TO 'UTC'")
        connection.execute(f"SET statement_timeout TO {self.statement_timeout_ms}")
        connection.execute(f"SET lock_timeout TO {self.lock_timeout_ms}")
        connection.execute("SET idle_in_transaction_session_timeout TO 60000")
        connection.commit()

    def open(self) -> PostgresDatabase:
        if not self._opened:
            self._pool.open(wait=True)
            self._opened = True
        return self

    def configure(self) -> PostgresDatabase:
        self.open()
        with self.connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('ipc-schema-v1'))"
            )
            for statement in SCHEMA_CONTRACT_STATEMENTS:
                connection.execute(statement)
        return self

    @contextmanager
    def connect(self) -> Generator[DatabaseConnection, None, None]:
        self.open()
        with self._pool.connection() as raw:
            with raw.transaction():
                yield DatabaseConnection(raw)

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False


Database = PostgresDatabase
