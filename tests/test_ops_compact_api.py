from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_dir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as test_client:
        setup_test_auth(test_client)
        test_client.app.state.ipc.config_dir = config_dir
        test_client.app.state.ipc.reload_config()
        yield test_client


def _configure(client: TestClient):
    response = client.put(
        "/api/ops/config",
        json={
            "api_format": "openai",
            "api_key": "ops-secret-key",
            "base_url": "https://api.openai.invalid/v1",
            "model": "openai-model",
        },
    )
    assert response.status_code == 200
    return client.app.state.ipc.ops_agent_service


def _long_session(service, turns: int = 12) -> str:
    """Create an Ops conversation with a real agent transcript behind it."""

    from backend.ops.agent_bridge import ensure_ops_agent_session

    session = service.store.create_session("compaction test")
    config = service.store.load_llm_config()
    agent = ensure_ops_agent_session(service.agent_store, service.store, session["id"], config)
    writer = service.agent_store.claim_writer(
        agent["id"], "ops-run", "test-seed", seconds=600, takeover=True
    )
    service.agent_store.append_event(
        agent["id"], "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "set up the i春秋 workflow"}]},
        owner=writer["owner"], epoch=writer["epoch"],
    )
    for index in range(turns):
        service.agent_store.append_event(
            agent["id"], f"provider:{index + 1}", "provider_messages",
            {"messages": [{"role": "assistant", "content": f"step {index} " + "x" * 300}]},
            owner=writer["owner"], epoch=writer["epoch"],
        )
    return session["id"]


def test_compact_endpoint_summarizes_the_conversation(client, monkeypatch):
    service = _configure(client)
    session_id = _long_session(service)

    def fake_compressor(config, *, instructions=""):
        def compress(_messages):
            return (
                {"verified_facts": [{"claim": "workflow wf_1 is confirmed", "evidence": "chat"}]},
                [{"role": "user", "content": "recent tail"}],
            )

        return compress

    monkeypatch.setattr("backend.ops.agent_bridge.build_ops_compressor", fake_compressor)

    response = client.post(
        f"/api/ops/sessions/{session_id}/compact", json={"instructions": "keep the workflow"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["compacted"] is True
    assert body["session_id"] == session_id
    assert body["messages_after"] < body["messages_before"]
    assert body["summary"]["verified_facts"][0]["claim"] == "workflow wf_1 is confirmed"

    # the compaction is durable: the next turn projects from the summary
    events = service.agent_store.events(service.store.agent_session_id(session_id), 0, 200)
    assert any(event["kind"] == "context_compacted" for event in events)


def test_compact_endpoint_accepts_an_empty_body(client, monkeypatch):
    service = _configure(client)
    session_id = service.store.create_session("tiny")["id"]

    def fake_compressor(config, *, instructions=""):
        return lambda messages: ({"verified_facts": []}, list(messages))

    monkeypatch.setattr("backend.ops.agent_bridge.build_ops_compressor", fake_compressor)

    response = client.post(f"/api/ops/sessions/{session_id}/compact")

    assert response.status_code == 200
    assert response.json()["compacted"] is False


def test_compact_endpoint_rejects_an_unknown_session(client):
    _configure(client)

    response = client.post("/api/ops/sessions/ops_0000000000000000/compact")

    assert response.status_code == 404
