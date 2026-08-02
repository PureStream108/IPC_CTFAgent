from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.blackboard.db import Database
from backend.core.state import AppState
from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import close_lifespan, create_mcp_server
from backend.memory.memory_store import MemoryStore
from backend.server.app import create_app
from backend.sqlite_util import RamSqlite
from backend.tools.tool_registry import ToolRegistry
from tests.helpers import setup_test_auth, write_mock_config


def test_ram_sqlite_shared_cache_is_thread_safe_and_close_is_idempotent(monkeypatch):
    monkeypatch.setattr("backend.sqlite_util._memdb_available", lambda: False)
    ram = RamSqlite("fallback")
    with ram.guard():
        conn = ram.connect()
        try:
            conn.execute("CREATE TABLE values_table (value INTEGER)")
            conn.commit()
        finally:
            conn.close()

    def insert(value: int) -> None:
        with ram.guard():
            conn = ram.connect()
            try:
                conn.execute("INSERT INTO values_table VALUES (?)", (value,))
                conn.commit()
            finally:
                conn.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(insert, range(40)))
    with ram.guard():
        conn = ram.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 40
        finally:
            conn.close()

    dsn = ram.dsn
    ram.close()
    ram.close()
    assert ram.closed
    with pytest.raises(RuntimeError, match="closed"):
        ram.connect()

    # Closing the keeper releases the old shared in-memory database. Opening
    # the same URI now creates a fresh empty database.
    fresh = sqlite3.connect(dsn, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            fresh.execute("SELECT * FROM values_table").fetchall()
    finally:
        fresh.close()


def test_storage_close_methods_are_idempotent_and_reject_ram_connections(tmp_path):
    database = Database(tmp_path / "graph.db", in_memory=True).configure()
    memory = MemoryStore(tmp_path / "memory.db", in_memory=True).configure()
    registry = ToolRegistry(
        cache_db=tmp_path / "tools.db", in_memory=True
    ).load()

    for resource in (registry, memory, database):
        resource.close()
        resource.close()

    with pytest.raises(RuntimeError, match="closed"):
        with database.connect():
            pass
    with pytest.raises(RuntimeError, match="closed"):
        with memory._connect():
            pass
    with pytest.raises(RuntimeError, match="closed"):
        with registry._conn():
            pass


def test_app_state_closes_owned_resources_in_dependency_order(tmp_path, monkeypatch):
    state = AppState(
        root=tmp_path,
        config_dir=write_mock_config(tmp_path / "config"),
    )
    order: list[str] = []
    registry_close = state.registry.close
    memory_close = state.memory.close
    database_close = state.db.close

    monkeypatch.setattr(
        state.registry,
        "close",
        lambda: (order.append("registry"), registry_close())[1],
    )
    monkeypatch.setattr(
        state.memory,
        "close",
        lambda: (order.append("memory"), memory_close())[1],
    )
    monkeypatch.setattr(
        state.db,
        "close",
        lambda: (order.append("database"), database_close())[1],
    )
    state.close()
    state.close()

    assert order[:3] == ["registry", "memory", "database"]
    with pytest.raises(RuntimeError, match="closed"):
        with state.db.connect():
            pass


def test_fastmcp_lifespan_closes_owned_resource_on_normal_and_error_exit():
    calls: list[str] = []
    server = create_mcp_server(
        "lifecycle-test",
        lifespan=close_lifespan(lambda: calls.append("closed")),
    )

    @server.tool()
    async def explode() -> str:
        raise RuntimeError("expected")

    async def run() -> None:
        with pytest.raises(Exception):
            async with MCPClient.in_process(server) as client:
                await client.call_tool("explode")

    asyncio.run(run())
    assert calls == ["closed"]


def test_fastapi_lifespan_closes_state_after_request_exception(tmp_path):
    app = create_app(root=tmp_path)

    @app.get("/test-lifespan-error")
    def fail_request():
        raise RuntimeError("expected request failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        setup_test_auth(client)
        state = client.app.state.ipc
        assert client.get("/test-lifespan-error").status_code == 500
        with state.db.connect() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1

    for ram in (state.registry._ram, state.memory._ram, state.db._ram):
        assert ram is not None and ram.closed
    with pytest.raises(RuntimeError, match="closed"):
        with state.db.connect():
            pass
