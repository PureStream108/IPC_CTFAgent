from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.blackboard import graph_store
from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as current:
        setup_test_auth(current)
        current.app.state.ipc.config_dir = cfgdir
        current.app.state.ipc.reload_config()
        yield current


def _project_with_wp(client, content="# One\n"):
    detail = client.post(
        "/projects",
        json={"title": "Live WP", "origin": "o", "goal": "g", "category": "web"},
    ).json()
    pid = detail["project"]["id"]
    path = client.app.state.ipc.wp_dir / f"{pid}.md"
    path.write_text(content, encoding="utf-8")
    with client.app.state.ipc.db.connect() as conn:
        graph_store.set_wp_path(conn, pid, str(path))
    return pid, path


def _read_event(lines) -> tuple[str, dict]:
    event = ""
    chunks: list[str] = []
    for line in lines:
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            chunks.append(line[6:])
        elif line == "" and event:
            return event, json.loads("\n".join(chunks))
    raise AssertionError("event stream ended before an event was available")


def test_project_wp_etag_and_304(client):
    pid, path = _project_with_wp(client, "# First\n")
    first = client.get(f"/projects/{pid}/wp")
    assert first.status_code == 200
    assert first.text == "# First\n"
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert first.headers["x-wp-etag"] == etag.strip('"')
    assert first.headers["cache-control"] == "no-cache"

    cached = client.get(f"/projects/{pid}/wp", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["x-wp-etag"] == etag.strip('"')

    path.write_text("# Second\n", encoding="utf-8")
    changed = client.get(f"/projects/{pid}/wp", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.text == "# Second\n"
    assert changed.headers["x-wp-etag"] != etag.strip('"')


def test_project_wp_stream_writeup_update_and_missing(client, monkeypatch):
    import asyncio

    from backend.api import wp as wp_api

    monkeypatch.setenv("IPC_WP_STREAM_INTERVAL", "0.25")
    pid, path = _project_with_wp(client, "# One\n")
    state = client.app.state.ipc

    response = asyncio.run(wp_api.stream_wp(pid, state=state))
    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache, no-store"

    async def scenario():
        initial = wp_api._project_wp_record(state, pid)
        stream = wp_api._wp_event_stream(state, pid, initial)
        first = await anext(stream)
        assert first.startswith("event: writeup\n")
        first_payload = json.loads(first.split("data: ", 1)[1])
        assert first_payload["content"] == "# One\n"
        first_etag = first_payload["etag"]

        path.write_text("# Two\n", encoding="utf-8")
        second = await anext(stream)
        second_payload = json.loads(second.split("data: ", 1)[1])
        assert second.startswith("event: writeup\n")
        assert second_payload["content"] == "# Two\n"
        assert second_payload["etag"] != first_etag

        path.unlink()
        missing = await anext(stream)
        missing_payload = json.loads(missing.split("data: ", 1)[1])
        assert missing.startswith("event: missing\n")
        assert missing_payload["wp_path"].endswith(".md")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(scenario())


def test_project_wp_stream_rejects_missing_project_or_writeup(client):
    assert client.get("/projects/missing/wp/stream").status_code == 404
    detail = client.post(
        "/projects",
        json={"title": "No WP", "origin": "o", "goal": "g", "category": "web"},
    ).json()
    pid = detail["project"]["id"]
    assert client.get(f"/projects/{pid}/wp/stream").status_code == 404
    assert client.get(f"/projects/{pid}/wp").status_code == 404


def test_no_global_writeup_meta_endpoint(client):
    assert client.get("/wp/completed/meta").status_code == 404
