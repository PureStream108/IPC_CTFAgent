from __future__ import annotations

import psycopg.errors
import pytest
from fastapi.testclient import TestClient

from backend.core import project_cache
from backend.core.project_cache import BoundedCache, configured_limit
from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config


def test_bounded_cache_evicts_oldest_and_tracks_hits():
    cache: BoundedCache[int] = BoundedCache(3, "test")
    for index in range(5):
        cache.put(f"k{index}", index)
    assert cache.keys() == ["k2", "k3", "k4"]
    assert len(cache) == 3
    assert cache.get("k2") == 2
    cache.put("k5", 5)
    assert cache.keys() == ["k4", "k2", "k5"]
    bookkeeping = cache.stats()
    assert bookkeeping["evictions"] == 3
    assert bookkeeping["hits"] == 1


def test_bounded_cache_limit_zero_disables_storage():
    cache: BoundedCache[str] = BoundedCache(0, "disabled")
    cache.put("a", "b")
    assert len(cache) == 0
    assert cache.get("a") is None


def test_configured_limit_reads_environment(monkeypatch):
    monkeypatch.delenv("IPC_PROJECT_CACHE_MAX", raising=False)
    assert configured_limit(64) == 64
    monkeypatch.setenv("IPC_PROJECT_CACHE_MAX", "12")
    assert configured_limit(64) == 12
    monkeypatch.setenv("IPC_PROJECT_CACHE_MAX", "0")
    assert configured_limit(64) == 0
    monkeypatch.setenv("IPC_PROJECT_CACHE_MAX", "not-a-number")
    assert configured_limit(64) == 64


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    project_cache.reset_caches()
    app = create_app(root=tmp_path)
    with TestClient(app) as test_client:
        setup_test_auth(test_client)
        test_client.app.state.ipc.config_dir = cfgdir
        test_client.app.state.ipc.reload_config()
        yield test_client
    project_cache.reset_caches()


def _create(client, title):
    response = client.post(
        "/projects",
        json={"title": title, "origin": "http://example.test", "goal": "get flag", "category": "web"},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]["id"]


@pytest.mark.postgres
def test_detail_cache_is_bounded_across_many_projects(client, monkeypatch):
    monkeypatch.setenv("IPC_PROJECT_CACHE_MAX", "64")
    project_cache.project_detail_cache.limit = 64
    ids = [_create(client, f"Cache probe {index}") for index in range(70)]
    for project_id in ids:
        assert client.get(f"/projects/{project_id}").status_code == 200
    assert len(project_cache.project_detail_cache) == 64
    assert ids[0] not in project_cache.project_detail_cache
    assert ids[-1] in project_cache.project_detail_cache


@pytest.mark.postgres
def test_deleted_project_is_not_served_from_cache(client, monkeypatch):
    project_id = _create(client, "Delete probe")
    assert client.get(f"/projects/{project_id}").status_code == 200
    assert project_id in project_cache.project_detail_cache

    assert client.delete(f"/projects/{project_id}").status_code == 204
    assert project_id not in project_cache.project_detail_cache
    assert client.get(f"/projects/{project_id}").status_code == 404


@pytest.mark.postgres
def test_detail_cache_still_serves_last_good_copy_during_outage(client, monkeypatch):
    project_id = _create(client, "Outage probe")
    assert client.get(f"/projects/{project_id}").status_code == 200
    cached = project_cache.project_detail_cache.get(project_id)
    assert cached is not None

    def unavailable(*args, **kwargs):
        raise psycopg.errors.OperationalError("database is down")

    monkeypatch.setattr("backend.blackboard.graph_store.project_detail", unavailable)
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["project"]["id"] == project_id
