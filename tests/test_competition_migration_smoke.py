from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from psycopg import connect
from psycopg.sql import Identifier, SQL


pytestmark = pytest.mark.postgres


def _dsn_for_database(base: str, database: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.netloc:
        raise ValueError("IPC_TEST_DATABASE_URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))


def test_old_revision_upgrades_forward_and_keeps_legacy_rows(monkeypatch):
    base = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("set IPC_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database_name = f"ipc_upgrade_{uuid.uuid4().hex[:12]}"
    maintenance = _dsn_for_database(base, "postgres")
    target = _dsn_for_database(base, database_name)
    with connect(maintenance, autocommit=True) as connection:
        connection.execute(SQL("CREATE DATABASE {} ").format(Identifier(database_name)))
    try:
        monkeypatch.setenv("IPC_DATABASE_URL", target)
        config = Config("alembic.ini")
        config.attributes["ipc_database_url"] = target
        # Keep both the repository-specific attribute and Alembic's standard
        # URL populated.  This also works with older Alembic runners that
        # clone Config before entering env.py.
        config.set_main_option("sqlalchemy.url", target)
        command.upgrade(config, "20260807_0002")
        with connect(target) as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,source,name,spec_json,spec_digest,created_at,updated_at)
                   VALUES ('legacy-workflow','legacy','Legacy','{}','legacy-digest',now(),now())"""
            )
            connection.commit()
        command.upgrade(config, "head")
        with connect(target) as connection:
            workflow = connection.execute(
                "SELECT id,name FROM workflows WHERE id='legacy-workflow'"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
                ).fetchall()
            }
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert workflow == ("legacy-workflow", "Legacy")
        assert {
            "competition_runs",
            "competition_challenges",
            "competition_sessions",
            "competition_artifacts",
            "competition_run_observations",
        }.issubset(tables)
        assert revision == "20260923_0007"
    finally:
        with connect(maintenance, autocommit=True) as connection:
            connection.execute(
                SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(database_name))
            )
