"""Cleanup must hold when migration or the test body fails."""
from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest

from tests import conftest as isolation

pytestmark = pytest.mark.postgres


def nested_request(request):
    return SimpleNamespace(
        node=SimpleNamespace(get_closest_marker=lambda _name: True),
        getfixturevalue=request.getfixturevalue,
    )


def capture_schemas(monkeypatch):
    created = []
    original = isolation._create_schema

    def create(dsn, schema):
        created.append(schema)
        original(dsn, schema)

    monkeypatch.setattr(isolation, "_create_schema", create)
    return created


def schema_exists(dsn, schema):
    with psycopg.connect(dsn, autocommit=True) as connection:
        return connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone() is not None


def test_migration_failure_removes_its_schema(request, monkeypatch):
    dsn = request.getfixturevalue("postgres_test_dsn")
    with monkeypatch.context() as nested:
        schemas = capture_schemas(nested)

        def fail(*_args):
            raise RuntimeError("migration failed")

        nested.setattr(isolation.command, "upgrade", fail)
        fixture = isolation.isolated_postgres.__wrapped__(nested_request(request), nested)
        with pytest.raises(RuntimeError, match="migration failed"):
            next(fixture)
    assert len(schemas) == 1
    assert not schema_exists(dsn, schemas[0])


def test_test_failure_closes_unclosed_pools_and_removes_schema(request, monkeypatch):
    from backend.persistence.database import Database

    dsn = request.getfixturevalue("postgres_test_dsn")
    with monkeypatch.context() as nested:
        schemas = capture_schemas(nested)
        fixture = isolation.isolated_postgres.__wrapped__(nested_request(request), nested)
        next(fixture)
        database = Database().configure()
        assert database._opened
        with pytest.raises(RuntimeError, match="test failed"):
            fixture.throw(RuntimeError("test failed"))
        assert database._pool.closed
        assert not database._opened
    assert not schema_exists(dsn, schemas[0])


def test_untracked_connection_is_reported_but_schema_is_still_removed(request, monkeypatch):
    import os

    dsn = request.getfixturevalue("postgres_test_dsn")
    with monkeypatch.context() as nested:
        schemas = capture_schemas(nested)
        fixture = isolation.isolated_postgres.__wrapped__(nested_request(request), nested)
        next(fixture)
        connection = psycopg.connect(os.environ["IPC_DATABASE_URL"], autocommit=True)
        try:
            with pytest.raises(AssertionError, match="retained connections"):
                fixture.close()
            assert not schema_exists(dsn, schemas[0])
        finally:
            connection.close()
