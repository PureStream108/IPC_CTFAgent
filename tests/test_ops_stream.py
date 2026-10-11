from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config


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


def _configure(client, **extra):
    payload = {
        "api_format": "openai",
        "api_key": "openai-secret-key",
        "base_url": "https://api.openai.invalid/v1",
        "model": "openai-model",
    }
    payload.update(extra)
    assert client.put("/api/ops/config", json=payload).status_code == 200
    return client.app.state.ipc.ops_agent_service


class _Tools:
    def catalog(self):
        return [
            {
                "name": "task_sandbox_health",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    def execute(self, name, arguments):
        return {"ok": True}


def _reasoning_adapter(turns):
    """A ConversationAdapter stand-in that streams reasoning and text."""

    class FakeAdapter:
        emits_reasoning = True

        def __init__(self, *args, **kwargs):
            FakeAdapter.instances.append(self)

        def turn(self, messages, system, tools, emit, cancel, emit_reasoning=None):
            reasoning, chunks, turn = turns.pop(0)
            for piece in reasoning:
                assert emit_reasoning is not None
                emit_reasoning(piece)
            for piece in chunks:
                emit(piece)
            return turn

        def tool_result(self, call, output):
            return {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(output, ensure_ascii=False),
            }

    FakeAdapter.instances = []
    return FakeAdapter


def _events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_carries_reasoning_and_text_increments(client, monkeypatch):
    """The reply must arrive as it is produced, not only inside ``complete``.

    Before this the emit callback was never wired into the Ops runtime, so
    every increment the provider produced was dropped at the source and the
    page could only refresh after the run finished.
    """
    from backend.competition.conversation import Turn

    service = _configure(client)
    adapter = _reasoning_adapter(
        [
            (
                ["thinking about ", "the sandbox"],
                ["Hello", " operator"],
                Turn([{"role": "assistant", "content": "Hello operator"}], [], "Hello operator"),
            )
        ]
    )
    monkeypatch.setattr("backend.ops.agent_bridge.ConversationAdapter", adapter)
    service.tools = _Tools()

    streamed = client.post("/api/ops/chat/stream", json={"message": "hi"})
    assert streamed.status_code == 200
    events = _events(streamed)

    kinds = [event["type"] for event in events]
    assert kinds[0] == "session"
    assert kinds[1] == "run"
    assert kinds[-1] == "complete"
    assert kinds.index("thinking") < kinds.index("delta") < kinds.index("complete")

    assert "".join(e["text"] for e in events if e["type"] == "thinking") == (
        "thinking about the sandbox"
    )
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello operator"
    complete = next(e["response"] for e in events if e["type"] == "complete")
    assert complete["reply"] == "Hello operator"


def test_increments_are_durable_and_resume_by_id(client, monkeypatch):
    """A reconnecting page re-reads increments by id without duplication."""
    from backend.competition.conversation import Turn

    service = _configure(client)
    adapter = _reasoning_adapter(
        [
            (
                ["why not"],
                ["partial ", "answer"],
                Turn([{"role": "assistant", "content": "partial answer"}], [], "partial answer"),
            )
        ]
    )
    monkeypatch.setattr("backend.ops.agent_bridge.ConversationAdapter", adapter)
    service.tools = _Tools()

    events = _events(client.post("/api/ops/chat/stream", json={"message": "hi"}))
    run_id = next(e["run_id"] for e in events if e["type"] == "run")
    session_id = next(e["session_id"] for e in events if e["type"] == "session")

    increments = [e for e in events if e["type"] in ("delta", "thinking")]
    assert increments, "the run produced no increments"
    ids = [e["id"] for e in increments]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)

    replay = service.store.run_stream_text(run_id)
    assert replay == {"text": "partial answer", "reasoning": "why not"}

    history = client.get(f"/api/ops/sessions/{session_id}").json()
    assert all(item["kind"] not in ("delta", "thinking") for item in history["events"])


def test_tool_payloads_never_reach_the_answer_stream(client, monkeypatch):
    """Commands and their output stay in the log, never in the reply bubble."""
    from backend.competition.conversation import ToolCall, Turn

    service = _configure(client)
    adapter = _reasoning_adapter(
        [
            (
                [],
                [],
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "call-1"}]}],
                    [ToolCall("call-1", "task_sandbox_health", {"project_id": "p1"})],
                    "",
                ),
            ),
            (
                [],
                ["checked"],
                Turn([{"role": "assistant", "content": "checked"}], [], "checked"),
            ),
        ]
    )
    monkeypatch.setattr("backend.ops.agent_bridge.ConversationAdapter", adapter)
    service.tools = _Tools()

    events = _events(client.post("/api/ops/chat/stream", json={"message": "probe"}))

    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert answer == "checked"
    assert "task_sandbox_health" not in answer
    logs = [e["event"] for e in events if e["type"] == "log"]
    assert any(item["label"] == "Tool · task_sandbox_health" for item in logs)
    assert any(item["label"] == "Tool result · task_sandbox_health" for item in logs)


def test_secret_values_are_redacted_across_increment_boundaries(client, monkeypatch):
    """A secret split over two increments must still be masked.

    Each increment is redacted on its own, so a flush boundary falling inside
    the secret would let the raw value through. The sink holds back a tail as
    long as the longest secret to make that impossible.
    """
    from backend.competition.conversation import Turn

    service = _configure(client)
    adapter = _reasoning_adapter(
        [
            (
                [],
                ["the token is sup", "er-secret-value done"],
                Turn(
                    [{"role": "assistant", "content": "the token is super-secret-value done"}],
                    [],
                    "the token is super-secret-value done",
                ),
            )
        ]
    )
    monkeypatch.setattr("backend.ops.agent_bridge.ConversationAdapter", adapter)
    service.tools = _Tools()

    streamed = client.post(
        "/api/ops/chat/stream",
        json={"message": "echo it", "secrets": {"api_token": "super-secret-value"}},
    )
    events = _events(streamed)
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "super-secret-value" not in answer
    assert "{{secret.api_token}}" in answer


def test_mock_format_keeps_the_synchronous_shape(client):
    """The offline mock has no streaming surface and must stay unchanged."""
    assert client.put(
        "/api/ops/config",
        json={
            "api_format": "mock",
            "api_key": "k",
            "base_url": "https://llm.invalid/v1",
            "model": "m",
        },
    ).status_code == 200

    events = _events(client.post("/api/ops/chat/stream", json={"message": "ping"}))
    assert [event["type"] for event in events] == ["session", "complete"]


def test_adapters_without_a_reasoning_channel_still_run(client, monkeypatch):
    """Five-argument adapters must keep working, reasoning or not."""
    from backend.competition.conversation import Turn

    service = _configure(client)
    calls: list[int] = []

    class FiveArgAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def turn(self, messages, system, tools, emit, cancel):
            calls.append(len(messages))
            emit("plain")
            return Turn([{"role": "assistant", "content": "plain"}], [], "plain")

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": "{}"}

    monkeypatch.setattr("backend.ops.agent_bridge.ConversationAdapter", FiveArgAdapter)
    service.tools = _Tools()

    events = _events(client.post("/api/ops/chat/stream", json={"message": "hi"}))
    assert calls
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "plain"
    assert not [e for e in events if e["type"] == "thinking"]
