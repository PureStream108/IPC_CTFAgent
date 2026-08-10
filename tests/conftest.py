from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


POSTGRES_TEST_MODULES = {
    "test_api.py",
    "test_auth.py",
    "test_blackboard.py",
    "test_catalog.py",
    "test_frontend.py",
    "test_goal_intent_postgres.py",
    "test_lifecycle.py",
    "test_members.py",
    "test_memory.py",
    "test_ops_agent.py",
    "test_optional_startup.py",
    "test_orchestration.py",
    "test_platform.py",
    "test_wp_writer.py",
}

_MIGRATED_DATABASE_URL: str | None = None

TRUNCATE_TABLES = (
    "migration_runs",
    "audit_events",
    "postprocess_jobs",
    "flag_submissions",
    "active_sessions",
    "workflows",
    "session_projects",
    "events",
    "runs",
    "messages",
    "sessions",
    "mem_counter",
    "memories",
    "scoped_counters",
    "counters",
    "broadcasts",
    "attachments",
    "reports",
    "agent_links",
    "agents",
    "hints",
    "intent_sources",
    "intents",
    "facts",
    "projects",
    "settings",
)


def pytest_collection_modifyitems(config, items) -> None:
    del config
    unavailable = pytest.mark.skip(
        reason="set IPC_TEST_DATABASE_URL to run PostgreSQL integration tests"
    )
    has_database = bool(os.environ.get("IPC_TEST_DATABASE_URL", "").strip())
    for item in items:
        if Path(str(item.fspath)).name not in POSTGRES_TEST_MODULES:
            continue
        item.add_marker("postgres")
        if not has_database:
            item.add_marker(unavailable)


@pytest.fixture(autouse=True)
def isolated_postgres(request, monkeypatch):
    if request.node.get_closest_marker("postgres") is None:
        yield
        return

    dsn = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if not dsn:
        yield
        return

    monkeypatch.setenv("IPC_DATABASE_URL", dsn)
    monkeypatch.setenv("IPC_DB_POOL_MIN", "1")
    monkeypatch.setenv("IPC_DB_POOL_MAX", "4")

    from backend.persistence.database import Database

    global _MIGRATED_DATABASE_URL
    if _MIGRATED_DATABASE_URL != dsn:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["ipc_database_url"] = dsn
        command.upgrade(config, "head")
        _MIGRATED_DATABASE_URL = dsn

    database = Database(dsn, min_size=1, max_size=4).configure()
    _truncate(database)
    database.close()
    try:
        yield
    finally:
        cleanup = Database(dsn, min_size=1, max_size=4).configure()
        _truncate(cleanup)
        cleanup.close()


def _truncate(database) -> None:
    with database.connect() as connection:
        connection.execute(
            "TRUNCATE TABLE " + ", ".join(TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE"
        )
