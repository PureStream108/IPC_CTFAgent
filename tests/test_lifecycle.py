from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.blackboard.db import Database
from backend.core.state import AppState
from backend.mcp.mcp_client import MCPClient
from backend.mcp.mcp_server import close_lifespan, create_mcp_server
from backend.memory.memory_store import MemoryStore
from backend.server.app import create_app
from backend.tools.tool_registry import ToolRegistry
from tests.helpers import setup_test_auth, write_mock_config


def test_postgres_pool_serves_concurrent_connections():
    database = Database(min_size=1, max_size=8).configure()

    def query(value: int) -> int:
        with database.connect() as connection:
            row = connection.execute("SELECT %s::integer AS value", (value,)).fetchone()
        return int(row["value"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(query, range(40))) == list(range(40))
    database.close()


def test_storage_close_methods_are_idempotent_and_clear_local_cache():
    database = Database().configure()
    memory = MemoryStore().configure()
    registry = ToolRegistry().load()
    registry._cache_results("query", ["tool"])

    for resource in (registry, memory, database):
        resource.close()
        resource.close()

    with pytest.raises(Exception, match="closed|reused"):
        with database.connect():
            pass
    with pytest.raises(Exception, match="closed|reused"):
        with memory.db.connect():
            pass
    assert registry.cached_search("query") is None


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
    with pytest.raises(Exception, match="closed|reused"):
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
            assert conn.execute("SELECT 1 AS value").fetchone()["value"] == 1

    assert state.registry._cache == {}
    with pytest.raises(Exception, match="closed|reused"):
        with state.db.connect():
            pass
