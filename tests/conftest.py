from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    "test_member_driver.py",
    "test_memory.py",
    "test_ops_agent.py",
    "test_ops_stream.py",
    "test_optional_startup.py",
    "test_orchestration.py",
    "test_platform.py",
    "test_platform_verdict.py",
    "test_wp_writer.py",
}


def _native_pg_ctl() -> str | None:
    if importlib.util.find_spec("pytest_postgresql") is None:
        return None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return None
    configured = os.environ.get("IPC_TEST_POSTGRES_EXECUTABLE") or shutil.which("pg_ctl")
    if configured and Path(configured).is_file():
        return configured
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            result = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, timeout=5, check=True)
        except (subprocess.SubprocessError, OSError):
            return None
        executable = Path(result.stdout.strip()) / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")
        if executable.is_file():
            return str(executable)
    return None


NATIVE_PG_CTL = _native_pg_ctl()
if NATIVE_PG_CTL:
    from pytest_postgresql.factories import postgresql_proc

    ipc_postgresql_proc = postgresql_proc(executable=NATIVE_PG_CTL, host="127.0.0.1", port=None, user="postgres", dbname="ipc_test_native")


@pytest.fixture(scope="session")
def postgres_test_dsn(request):
    configured = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if configured:
        yield configured
        return
    if NATIVE_PG_CTL is None:
        pytest.skip("install native PostgreSQL or set IPC_TEST_DATABASE_URL")
    process = request.getfixturevalue("ipc_postgresql_proc")
    from urllib.parse import quote

    password = f":{quote(process.password, safe='')}" if process.password else ""
    dsn = f"postgresql://{quote(process.user, safe='')}{password}@{process.host}:{process.port}/{getattr(process, 'maintenance_dbname', 'postgres')}"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("IPC_TEST_DATABASE_URL", dsn)
        yield dsn


@pytest.fixture(autouse=True)
def allow_local_sandbox(monkeypatch):
    """Tests run without Docker, so they opt into the host-backed sandbox.

    Production refuses ``sandbox_backend: local`` unless this flag is set, which
    keeps solver commands inside the task container. See
    tests/test_sandbox_containment.py for that guard.
    """
    monkeypatch.setenv("IPC_ALLOW_LOCAL_SANDBOX", "1")
    monkeypatch.delenv("IPC_ARTIFACT_ROOT", raising=False)


def pytest_addoption(parser):
    parser.addoption("--require-postgres", action="store_true", default=False,
                     help="fail collection instead of skipping unavailable PostgreSQL integration tests")


def pytest_collection_modifyitems(config, items) -> None:
    unavailable = pytest.mark.skip(
        reason="install native PostgreSQL or set IPC_TEST_DATABASE_URL"
    )
    has_database = bool(os.environ.get("IPC_TEST_DATABASE_URL", "").strip()) or NATIVE_PG_CTL is not None
    for item in items:
        if (
            Path(str(item.fspath)).name not in POSTGRES_TEST_MODULES
            and item.get_closest_marker("postgres") is None
        ):
            continue
        item.add_marker("postgres")
        if not has_database:
            if config.getoption("--require-postgres"):
                raise pytest.UsageError("PostgreSQL integration is required: install native PostgreSQL or set IPC_TEST_DATABASE_URL")
            item.add_marker(unavailable)


@pytest.fixture(autouse=True)
def isolated_postgres(request, monkeypatch):
    if request.node.get_closest_marker("postgres") is None:
        yield
        return

    dsn = request.getfixturevalue("postgres_test_dsn")

    schema = "test_" + uuid.uuid4().hex
    _create_schema(dsn, schema)
    isolated_dsn = _with_search_path(dsn, schema)
    monkeypatch.setenv("IPC_DATABASE_URL", isolated_dsn)
    monkeypatch.setenv("IPC_DB_POOL_MIN", "1")
    monkeypatch.setenv("IPC_DB_POOL_MAX", "4")

    from backend.persistence.database import Database
    databases = []
    original_init = Database.__init__

    def tracked_init(database, *args, **kwargs):
        original_init(database, *args, **kwargs)
        databases.append(database)

    monkeypatch.setattr(Database, "__init__", tracked_init)
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["ipc_database_url"] = isolated_dsn
        command.upgrade(config, "head")
        database = Database(isolated_dsn, min_size=1, max_size=4).configure()
        database.close()
        yield
    finally:
        try:
            for database in databases:
                database.close()
            _assert_no_connections(dsn, schema)
        finally:
            _drop_schema(dsn, schema)


def _create_schema(dsn: str, schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))


def _drop_schema(dsn: str, schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone() is None


def _assert_no_connections(dsn: str, schema: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        until = time.monotonic() + 2
        while True:
            remaining = connection.execute(
                "SELECT pid,state FROM pg_stat_activity WHERE application_name=%s", (schema,),
            ).fetchall()
            if not remaining or time.monotonic() >= until:
                break
            time.sleep(0.05)
    assert not remaining, f"test schema {schema} retained connections after pool teardown: {remaining}"


def _with_search_path(dsn: str, schema: str) -> str:
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    query["application_name"] = schema
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
