from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path

from backend.persistence.database import PostgresDatabase, sqlalchemy_database_url
from backend.persistence.schema import (
    SCHEMA_COMPATIBILITY_STATEMENTS,
    SCHEMA_CONTRACT_STATEMENTS,
    SCHEMA_STATEMENTS,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, params=()):
        del params
        self.statements.append(statement)
        return self


class _RecordingDatabase(PostgresDatabase):
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def open(self):
        return self

    @contextmanager
    def connect(self):
        yield self.connection


class _RecordingAlembicOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_runtime_and_alembic_use_the_same_ordered_schema_contract() -> None:
    assert SCHEMA_CONTRACT_STATEMENTS == (
        *SCHEMA_COMPATIBILITY_STATEMENTS,
        *SCHEMA_STATEMENTS,
    )
    assert "ADD COLUMN IF NOT EXISTS lease_owner" in "\n".join(
        SCHEMA_COMPATIBILITY_STATEMENTS
    )

    database = _RecordingDatabase().configure()
    assert database.connection.statements[0].startswith("SELECT pg_advisory_xact_lock")
    assert database.connection.statements[1:] == list(SCHEMA_CONTRACT_STATEMENTS)

    migration = _load_migration(
        "20260807_0001_postgres_core.py", "ipc_postgres_core_migration"
    )
    recorder = _RecordingAlembicOp()
    migration.op = recorder
    migration.upgrade()
    assert recorder.statements == list(SCHEMA_CONTRACT_STATEMENTS)


def _load_migration(filename: str, module_name: str):
    migration_path = Path("backend/persistence/migrations/versions") / filename
    spec = importlib.util.spec_from_file_location(module_name, migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_goal_intent_uniqueness_is_an_explicit_audited_migration() -> None:
    migration = _load_migration(
        "20260807_0002_goal_intent_uniqueness.py",
        "ipc_goal_intent_uniqueness_migration",
    )

    assert migration.down_revision == "20260807_0001"
    recorder = _RecordingAlembicOp()
    migration.op = recorder
    migration.upgrade()

    combined = "\n".join(recorder.statements)
    assert recorder.statements[0].strip() == (
        "LOCK TABLE intents IN SHARE ROW EXCLUSIVE MODE"
    )
    assert "migration.goal_intent_deduplicated" in combined
    assert "INSERT INTO intent_sources" in combined
    assert "UPDATE agent_links" in combined
    assert "UPDATE reports" in combined
    assert "DELETE FROM intents" in combined
    assert "CREATE UNIQUE INDEX uq_intents_one_goal_per_project" in combined
    assert "WHERE to_fact_id = 'goal'" in combined
    # Runtime bootstrap may create tables, but must never perform destructive
    # history cleanup or bypass this Alembic revision.
    assert "uq_intents_one_goal_per_project" not in "\n".join(
        SCHEMA_CONTRACT_STATEMENTS
    )


def test_goal_intent_uniqueness_downgrade_only_removes_the_index() -> None:
    migration = _load_migration(
        "20260807_0002_goal_intent_uniqueness.py",
        "ipc_goal_intent_uniqueness_downgrade",
    )
    recorder = _RecordingAlembicOp()
    migration.op = recorder

    migration.downgrade()

    assert recorder.statements == [
        "DROP INDEX IF EXISTS uq_intents_one_goal_per_project"
    ]


def test_sqlalchemy_url_normalizes_both_postgres_schemes(monkeypatch) -> None:
    monkeypatch.delenv("IPC_DATABASE_URL", raising=False)
    assert sqlalchemy_database_url("postgresql://user:pass@db/app") == (
        "postgresql+psycopg://user:pass@db/app"
    )
    assert sqlalchemy_database_url("postgres://user:pass@db/app") == (
        "postgresql+psycopg://user:pass@db/app"
    )
    assert sqlalchemy_database_url("postgresql+psycopg://user:pass@db/app") == (
        "postgresql+psycopg://user:pass@db/app"
    )


def test_database_url_accepts_explicit_psycopg_dsn(monkeypatch) -> None:
    from backend.persistence.database import database_url

    monkeypatch.setenv("IPC_DATABASE_URL", "postgresql://env-user:pass@db/ipc")
    assert database_url("postgresql+psycopg://user:pass@db/app") == (
        "postgresql://user:pass@db/app"
    )


def test_database_url_uses_environment_for_legacy_path_argument(monkeypatch) -> None:
    from backend.persistence.database import database_url

    monkeypatch.setenv("IPC_DATABASE_URL", "postgresql://env-user:pass@db/ipc")
    assert database_url(Path("legacy.sqlite")) == "postgresql://env-user:pass@db/ipc"


def test_alembic_env_prefers_explicit_config_url_when_environment_is_empty(monkeypatch):
    monkeypatch.delenv("IPC_DATABASE_URL", raising=False)
    migration_env = _load_migration_env_for_test()
    migration_env.config.attributes["ipc_database_url"] = "postgresql://user:pass@db/app"

    assert migration_env._migration_database_url() == (
        "postgresql+psycopg://user:pass@db/app"
    )


def _load_migration_env_for_test():
    # ``env.py`` calls Alembic's context at import time, so provide a minimal
    # fake context and config through the module loader only for this unit test.
    import sys
    import types

    from contextlib import nullcontext

    context = types.SimpleNamespace(config=types.SimpleNamespace(config_file_name=None))
    context.config.attributes = {}
    context.is_offline_mode = lambda: True
    context.configure = lambda **_kwargs: None
    context.begin_transaction = lambda: nullcontext()
    context.run_migrations = lambda: None
    fake_alembic = types.SimpleNamespace(context=context)
    previous = sys.modules.get("alembic")
    sys.modules["alembic"] = fake_alembic
    try:
        migration_path = Path("backend/persistence/migrations/env.py")
        spec = importlib.util.spec_from_file_location("ipc_migration_env_test", migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = previous
