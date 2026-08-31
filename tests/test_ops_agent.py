from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

import pytest
import requests
from fastapi.testclient import TestClient

from backend.blackboard import graph_store
from backend.core.config import LLMConfig
from backend.members.adapters import (
    ClaudeAdapter,
    DecisionOutputError,
    NonRetryableProviderError,
    OpenAICompatibleAdapter,
)
from backend.mcp.mcp_client import MCPClient
from backend.ops.network import NetworkPolicyError, WorkflowHttpClient
from backend.ops.ipc_mcp import build_ipc_mcp
from backend.ops.service import _claude_log_events, _parse_tool_call
from backend.ops.store import OpsStore
from backend.ops.tools import OpsToolExecutor
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


def configure_mock(client: TestClient):
    response = client.put(
        "/api/ops/config",
        json={
            "api_format": "mock",
            "api_key": "ops-secret-key",
            "base_url": "https://llm.invalid/v1",
            "model": "ops-model",
        },
    )
    assert response.status_code == 200
    return response


def test_tool_call_accepts_json_string_arguments():
    assert _parse_tool_call(
        {
            "name": "host_exec",
            "arguments": '{"command":"ls -la","timeout":15}',
        }
    ) == ("host_exec", {"command": "ls -la", "timeout": 15})


def test_claude_log_events_redact_tool_output_and_render_tools():
    events = _claude_log_events(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "echo secret-value"}},
                ]
            },
        },
        {"platform_token": "secret-value"},
    )
    assert events == [{"kind": "tool", "label": "Tool · Bash", "text": '{"command": "echo {{secret.platform_token}}"}'}]


def test_chat_stream_forwards_live_events_and_final_response(client, monkeypatch):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "claudecode",
            "api_key": "ops-secret-key",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
    )
    service = client.app.state.ipc.ops_agent_service

    class FakeRunner:
        enabled = True

        def stream(self, **kwargs):
            yield {"type": "started", "message": "started"}
            yield {
                "type": "event",
                "event": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "id"}},
                        ]
                    },
                },
            }
            yield {
                "type": "result",
                "reply": json.dumps({"reply": "log visible", "workflow": None}),
                "tool_events": [{"name": "Bash"}],
            }

    service.claude_runner = FakeRunner()
    response = client.post("/api/ops/chat/stream", json={"message": "run a safe test"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[0]["type"] == "session"
    assert events[1]["type"] == "run"
    assert events[1]["run_id"].startswith("run_")
    assert any(item["type"] == "log" and item["event"]["label"] == "Tool · Bash" for item in events)
    completed = next(item for item in events if item["type"] == "complete")
    assert completed["response"]["reply"] == "log visible"
    assert completed["response"]["tool_calls"] == [{"name": "Bash"}]
    history = client.get(f"/api/ops/sessions/{completed['response']['session_id']}")
    assert history.status_code == 200
    assert any(event["label"] == "Tool · Bash" for event in history.json()["events"])


def test_openai_chat_stream_runs_in_background_with_live_tool_events(client, monkeypatch):
    response = client.put(
        "/api/ops/config",
        json={
            "api_format": "openai",
            "api_surface": "responses",
            "api_key": "openai-secret-key",
            "base_url": "https://api.openai.invalid/v1",
            "model": "openai-model",
        },
    )
    assert response.status_code == 200
    service = client.app.state.ipc.ops_agent_service
    calls = []
    answers = iter(
        [
            json.dumps(
                {
                    "reply": "",
                    "workflow": None,
                    "tool_call": {
                        "name": "task_sandbox_health",
                        "arguments": {"project_id": "proj_openai"},
                    },
                }
            ),
            json.dumps(
                {
                    "reply": "OpenAI action completed",
                    "workflow": None,
                    "tool_call": None,
                }
            ),
        ]
    )

    class FakeAdapter:
        def chat(self, messages, **kwargs):
            calls.append((list(messages), kwargs))
            return next(answers)

    class FakeTools:
        def execute(self, name, arguments):
            assert name == "task_sandbox_health"
            assert arguments == {"project_id": "proj_openai"}
            return {"ok": True, "status": "running"}

    monkeypatch.setattr("backend.ops.service.make_adapter", lambda *args, **kwargs: FakeAdapter())
    service.tools = FakeTools()

    streamed = client.post("/api/ops/chat/stream", json={"message": "inspect the sandbox"})

    assert streamed.status_code == 200
    events = [json.loads(line) for line in streamed.text.splitlines() if line.strip()]
    assert events[0]["type"] == "session"
    assert events[1]["type"] == "run"
    assert events[1]["run_id"].startswith("run_")
    logs = [item["event"] for item in events if item["type"] == "log"]
    assert any(item["text"] == "OpenAI-compatible IPC started" for item in logs)
    assert any(item["label"] == "Tool · task_sandbox_health" for item in logs)
    assert any(item["label"] == "Tool result · task_sandbox_health" for item in logs)
    completed = next(item["response"] for item in events if item["type"] == "complete")
    assert completed["reply"] == "OpenAI action completed"
    assert completed["tool_calls"] == [
        {"name": "task_sandbox_health", "project_id": "proj_openai", "ok": True}
    ]
    assert len(calls) == 2
    assert "TOOL_RESULT task_sandbox_health" in calls[1][0][-1]["content"]
    history = client.get(f"/api/ops/sessions/{completed['session_id']}").json()
    assert history["active_run"] is None
    assert history["context_mode"] == "ipc_history"
    assert history["agent_context_ready"] is True
    assert [item["content"] for item in history["messages"]] == [
        "inspect the sandbox",
        "OpenAI action completed",
    ]


def test_openai_chat_stream_can_be_interrupted_without_claude_runner(client, monkeypatch):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "openai",
            "api_key": "openai-secret-key",
            "base_url": "https://api.openai.invalid/v1",
            "model": "openai-model",
        },
    )
    service = client.app.state.ipc.ops_agent_service
    entered = threading.Event()
    release = threading.Event()

    class FakeAdapter:
        def chat(self, messages, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return json.dumps({"reply": "must be discarded", "workflow": None})

    class DisabledRunner:
        enabled = False

    monkeypatch.setattr("backend.ops.service.make_adapter", lambda *args, **kwargs: FakeAdapter())
    service.claude_runner = DisabledRunner()
    follower = service.chat_stream(message="start an OpenAI action")
    session_event = next(follower)
    run_event = next(follower)
    assert entered.wait(timeout=3)

    interrupted = client.post(
        "/api/ops/chat/interrupt",
        json={
            "session_id": session_event["session_id"],
            "run_id": run_event["run_id"],
        },
    )
    assert interrupted.status_code == 200
    assert interrupted.json() == {
        "ok": True,
        "run_id": run_event["run_id"],
        "status": "interrupting",
    }
    release.set()
    remaining = list(follower)
    completed = next(item["response"] for item in remaining if item["type"] == "complete")
    assert completed["interrupted"] is True
    assert "OpenAI 运行日志已保存" in completed["reply"]
    history = client.get(f"/api/ops/sessions/{session_event['session_id']}").json()
    assert history["active_run"] is None
    assert "must be discarded" not in json.dumps(history, ensure_ascii=False)


def test_interrupt_chat_requests_runner_cancellation_and_persists_status(client):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "claudecode",
            "api_key": "ops-secret-key",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
    )
    service = client.app.state.ipc.ops_agent_service

    class FakeRunner:
        enabled = True

        def cancel(self, run_id):
            assert run_id == "run_0123456789abcdef"
            return {"ok": True, "run_id": run_id, "status": "interrupting"}

    service.claude_runner = FakeRunner()
    session_id = service.store.create_session("interrupt") ["id"]
    # Linked project events are mirrored through IPCLogger.  The event's own
    # ``kind`` field must not collide with the logger's destination argument.
    service.store.link_session_project(session_id, "proj_interrupt")
    service.store.create_run(session_id, "run_0123456789abcdef")
    response = client.post(
        "/api/ops/chat/interrupt",
        json={"session_id": session_id, "run_id": "run_0123456789abcdef"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "interrupting"
    active = client.get(f"/api/ops/sessions/{session_id}").json()["active_run"]
    assert active["id"] == "run_0123456789abcdef"
    assert active["cancel_requested"] is True
    events = client.get(f"/api/ops/sessions/{session_id}").json()["events"]
    assert events[-1]["text"] == "Operator requested interruption"


def test_active_ipc_conversation_cannot_be_deleted(client):
    assert client.get("/api/ops/config").status_code == 200
    service = client.app.state.ipc.ops_agent_service
    session_id = service.store.create_session("active") ["id"]
    service.store.create_run(session_id, "run_abcdef0123456789")

    response = client.delete(f"/api/ops/sessions/{session_id}")

    assert response.status_code == 409
    assert "interrupt" in response.json()["detail"]
    assert service.store.get_session(session_id)["id"] == session_id


def test_interrupted_claude_stream_finishes_the_chat_normally(client):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "claudecode",
            "api_key": "ops-secret-key",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
    )
    service = client.app.state.ipc.ops_agent_service

    class FakeRunner:
        enabled = True

        def stream(self, **kwargs):
            assert kwargs["run_id"].startswith("run_")
            yield {"type": "started", "message": "started"}
            yield {"type": "interrupted", "message": "IPC interrupted by operator"}

    service.claude_runner = FakeRunner()
    response = client.post("/api/ops/chat/stream", json={"message": "long running task"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    completed = next(item["response"] for item in events if item["type"] == "complete")
    assert completed["interrupted"] is True
    history = client.get(f"/api/ops/sessions/{completed['session_id']}").json()
    assert history["messages"][-1]["content"] == "IPC 已被操作员打断；已产生的实时日志已保存。"


def test_claude_stream_resumes_the_native_session_without_replaying_history(client):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "claudecode",
            "api_key": "ops-secret-key",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
    )
    service = client.app.state.ipc.ops_agent_service
    calls = []
    native_session_id = "05801f61-7b2c-4a5a-8db8-6cb1a7d54914"

    class FakeRunner:
        enabled = True

        def stream(self, **kwargs):
            calls.append(kwargs)
            yield {"type": "started", "message": "started"}
            yield {"type": "claude_session", "session_id": native_session_id}
            yield {
                "type": "result",
                "session_id": native_session_id,
                "reply": json.dumps({"reply": f"answer {len(calls)}", "workflow": None}),
            }

    service.claude_runner = FakeRunner()
    first = client.post("/api/ops/chat/stream", json={"message": "remember alpha"})
    first_events = [json.loads(line) for line in first.text.splitlines() if line.strip()]
    session_id = first_events[0]["session_id"]
    second = client.post(
        "/api/ops/chat/stream",
        json={"session_id": session_id, "message": "what did I ask?"},
    )

    assert second.status_code == 200
    assert calls[0]["resume_session_id"] is None
    assert calls[0]["prompt"] == "remember alpha"
    assert calls[1]["resume_session_id"] == native_session_id
    assert calls[1]["prompt"] == "what did I ask?"
    view = client.get(f"/api/ops/sessions/{session_id}").json()
    assert view["agent_context_ready"] is True
    assert view["active_run"] is None
    assert [item["content"] for item in view["messages"]] == [
        "remember alpha",
        "answer 1",
        "what did I ask?",
        "answer 2",
    ]


def test_claude_background_run_survives_stream_follower_disconnect(client):
    client.put(
        "/api/ops/config",
        json={
            "api_format": "claudecode",
            "api_key": "ops-secret-key",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
        },
    )
    service = client.app.state.ipc.ops_agent_service
    release = threading.Event()

    class FakeRunner:
        enabled = True

        def stream(self, **kwargs):
            yield {"type": "started", "message": "started"}
            assert release.wait(timeout=3)
            yield {
                "type": "result",
                "session_id": "afa728e4-6494-4663-89db-e6f2b07ff372",
                "reply": json.dumps({"reply": "finished in background", "workflow": None}),
            }

    service.claude_runner = FakeRunner()
    follower = service.chat_stream(message="keep working")
    session_event = next(follower)
    run_event = next(follower)
    follower.close()
    release.set()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if service.store.get_run(run_event["run_id"])["status"] == "completed":
            break
        time.sleep(0.02)
    run = service.store.get_run(run_event["run_id"])
    assert run["status"] == "completed"
    assert service.session_view(session_event["session_id"])["messages"][-1]["content"] == "finished in background"


def test_ops_store_uses_shared_postgres_schema(client):
    state = client.app.state.ipc
    store = OpsStore(state.root, state.db)
    with state.db.connect() as connection:
        tables = {
            row["table_name"]
            for row in connection.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('sessions', 'events', 'runs', 'workflows')
                """
            )
        }
    assert tables == {"sessions", "events", "runs", "workflows"}
    assert store.db is state.db


def test_ipc_mcp_finalizes_project_with_real_writeup(client):
    server = build_ipc_mcp(lambda: client.app.state.ipc)

    async def run():
        async with MCPClient.in_process(server) as mcp:
            created = await mcp.call_tool(
                "ipc_start_challenge",
                {
                    "title": "MCP archived challenge",
                    "category": "web",
                    "origin": "http://challenge.invalid",
                },
            )
            return await mcp.call_tool(
                "ipc_finalize_challenge",
                {
                    "project_id": created["project_id"],
                    "flag": "flag{archive}",
                    "markdown": """# MCP archived challenge

## Summary

The IPC action agent recorded a deterministic, reproducible demonstration solve.

## Solution

```python
import re


def main() -> None:
    output = "service returned flag{archive}"
    match = re.search(r"flag\\{[^}]+\\}", output)
    if not match:
        raise SystemExit("flag not found")
    print(match.group(0))


if __name__ == "__main__":
    main()
```

## Flag

`flag{archive}`
""",
                },
            )

    completed = asyncio.run(run())
    assert completed["ok"] is True
    state = client.app.state.ipc
    # Finalization commits the verified flag and writeup synchronously. Memory
    # and archive generation are durable, fenced background jobs, so the MCP
    # response intentionally does not contain the old synchronous ``archive``
    # object.  The worker may already have advanced the aggregate status by the
    # time the response is serialized; all these states are valid observations.
    assert completed["status"] == "solved"
    assert completed["postprocess_status"] in {"pending", "running", "completed"}
    assert completed["wp_path"]
    assert "archive" not in completed
    assert Path(completed["wp_path"]).is_file()

    # Wait for the asynchronous memory/archive workers before asserting their
    # filesystem contract.  A polling loop keeps this test deterministic on
    # both a local pool and a slower CI PostgreSQL instance.
    deadline = time.monotonic() + 8
    postprocess = None
    while time.monotonic() < deadline:
        with state.db.connect() as connection:
            postprocess = connection.execute(
                "SELECT postprocess_status FROM projects WHERE id = %s",
                (completed["project_id"],),
            ).fetchone()
        if postprocess and postprocess["postprocess_status"] == "completed":
            break
        time.sleep(0.05)
    assert postprocess is not None
    assert postprocess["postprocess_status"] == "completed"
    archived_wp = client.get("/wp/completed").json()["writeups"]
    assert any(item.get("project_id") == completed["project_id"] for item in archived_wp)
    writeups = client.get("/wp/completed").json()["writeups"]
    assert any(item["project_id"] == completed["project_id"] for item in writeups)

    # Simulate a fresh runtime: durable archives remain visible in
    # the same Logs/WP pages even when the live graph no longer has the project.
    with state.db.connect() as connection:
        connection.execute("DELETE FROM projects WHERE id = %s", (completed["project_id"],))
    archived_wp = client.get("/wp/completed").json()["writeups"]
    assert any(
        item.get("archived") is True
        and item.get("source_project_id") == completed["project_id"]
        for item in archived_wp
    )
    archived_logs = client.get("/logs/projects").json()["logs"]
    assert any(
        item.get("archived") is True
        and item.get("source_project_id") == completed["project_id"]
        for item in archived_logs
    )


def test_ipc_mcp_flag_conflict_does_not_overwrite_existing_writeup(client):
    state = client.app.state.ipc
    server = build_ipc_mcp(lambda: state)
    first_markdown = """# MCP conflict test

## Summary

This fixture preserves the first verified writeup when a conflicting finalizer loses.

## Solution

```python
import re


def main() -> None:
    output = "service returned flag{first}"
    match = re.search(r"flag\\{[^}]+\\}", output)
    if not match:
        raise SystemExit("flag not found")
    print(match.group(0))


if __name__ == "__main__":
    main()
```

## Flag

`flag{first}`
"""
    conflicting_markdown = first_markdown.replace("flag{first}", "flag{second}")

    async def run():
        async with MCPClient.in_process(server) as mcp:
            created = await mcp.call_tool(
                "ipc_start_challenge",
                {
                    "title": "MCP conflict test",
                    "category": "web",
                    "origin": "http://challenge.invalid",
                },
            )
            project_id = created["project_id"]
            first = await mcp.call_tool(
                "ipc_finalize_challenge",
                {"project_id": project_id, "flag": "flag{first}", "markdown": first_markdown},
            )
            conflict = await mcp.call_tool(
                "ipc_finalize_challenge",
                {
                    "project_id": project_id,
                    "flag": "flag{second}",
                    "markdown": conflicting_markdown,
                },
            )
            return project_id, first, conflict

    project_id, first, conflict = asyncio.run(run())
    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["conflict"] is True
    path = Path(first["wp_path"])
    assert path.read_text(encoding="utf-8") == first_markdown.rstrip() + "\n"
    with state.db.connect() as connection:
        project = connection.execute(
            "SELECT flag, wp_path FROM projects WHERE id = %s", (project_id,)
        ).fetchone()
    assert project == {"flag": "flag{first}", "wp_path": str(path)}


def workflow_payload(*, private: bool = True) -> dict:
    return {
        "name": "Example CTF",
        "challenges": {
            "list_url": "http://127.0.0.1:9000/api/challenges",
            "list_path": "data",
            "id_field": "id",
            "title_field": "name",
            "category_field": "category",
            "description_field": "description",
            "attachments_field": "files",
            "attachment_base_url": "http://127.0.0.1:9000/assets/",
            "headers": [
                {
                    "name": "Authorization",
                    "secret_name": "platform_token",
                    "prefix": "Token ",
                }
            ],
        },
        "submit": {
            "url": "http://127.0.0.1:9000/api/challenges/{{external_id}}/submit",
            "method": "POST",
            "headers": [
                {
                    "name": "Authorization",
                    "secret_name": "platform_token",
                    "prefix": "Token ",
                }
            ],
            "json_template": {"answer": "{{flag}}"},
            "success_statuses": [200],
            "success_path": "success",
            "success_values": [True],
        },
        "allow_private_networks": private,
    }


def test_ops_config_is_independent_persistent_and_redacted(client):
    response = configure_mock(client)
    body = response.json()
    assert body["configured"] is True
    assert body["api_key_set"] is True
    assert "ops-secret-key" not in response.text

    root = client.app.state.ipc.root / "data" / "ops-agent"
    assert "ops-secret-key" not in (root / "config.json").read_text(encoding="utf-8")
    assert "ops-secret-key" in (root / "secrets.json").read_text(encoding="utf-8")

    service = client.app.state.ipc.ops_agent_service
    reloaded = type(service)(client.app.state.ipc)
    assert reloaded.config_view()["model"] == "ops-model"
    assert "ops-secret-key" not in str(reloaded.config_view())


def test_chat_is_multi_turn_and_structured_secrets_never_enter_history(client):
    configure_mock(client)
    first = client.post(
        "/api/ops/chat",
        json={
            "message": "Use abcdefghijklmnop as the platform credential",
            "secrets": {"platform_token": "abcdefghijklmnop"},
        },
    )
    assert first.status_code == 200
    assert "abcdefghijklmnop" not in first.text
    assert "{{secret.platform_token}}" in first.json()["reply"]
    session_id = first.json()["session_id"]

    second = client.post(
        "/api/ops/chat",
        json={"session_id": session_id, "message": "What did I ask you to configure?"},
    )
    assert second.status_code == 200
    history = client.get(f"/api/ops/sessions/{session_id}")
    assert history.status_code == 200
    assert len(history.json()["messages"]) == 4
    assert "abcdefghijklmnop" not in history.text

    with client.app.state.ipc.db.connect() as connection:
        stored = " ".join(
            str(value)
            for row in connection.execute(
                "SELECT content FROM messages WHERE session_id = %s", (session_id,)
            )
            for value in row.values()
        )
    assert "abcdefghijklmnop" not in stored
    secret_file = client.app.state.ipc.root / "data" / "ops-agent" / "secrets.json"
    assert "abcdefghijklmnop" in secret_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "message",
    [
        "platform token: abcdefghijklmnop",
        "platform token abcdefghijklmnop",
        "平台 token是abcdefghijklmnop",
        "Cookie: session=abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnop",
    ],
)
def test_chat_accepts_direct_credentials(client, message):
    configure_mock(client)
    response = client.post(
        "/api/ops/chat",
        json={"message": message},
    )
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    history = client.get(f"/api/ops/sessions/{session_id}").json()
    assert message in [item["content"] for item in history["messages"]]


def test_agent_can_propose_a_workflow_without_seeing_secret(client, monkeypatch):
    configure_mock(client)
    seen_messages = []

    class FakeAdapter:
        def chat(self, messages, **kwargs):
            seen_messages.extend(messages)
            return json.dumps(
                {
                    "reply": "I prepared a reviewable workflow draft.",
                    "workflow": workflow_payload(),
                }
            )

    monkeypatch.setattr("backend.ops.service.make_adapter", lambda *args, **kwargs: FakeAdapter())
    response = client.post(
        "/api/ops/chat",
        json={
            "message": "Create the integration using abcdefghijklmnop",
            "secrets": {"platform_token": "abcdefghijklmnop"},
        },
    )
    assert response.status_code == 200
    assert len(response.json()["proposals"]) == 1
    assert response.json()["proposals"][0]["status"] == "draft"
    assert response.json()["proposals"][0]["secrets"] == [
        {"name": "platform_token", "secret_set": True}
    ]
    assert "abcdefghijklmnop" not in json.dumps(seen_messages)
    assert "abcdefghijklmnop" not in response.text


def test_ops_tools_are_exposed_and_chat_runs_a_tool_loop(client, monkeypatch):
    configure_mock(client)
    tools_response = client.get("/api/ops/tools")
    assert tools_response.status_code == 200
    definitions = {item["name"]: item for item in tools_response.json()["tools"]}
    assert {"list_task_sandboxes", "task_sandbox_health", "task_sandbox_exec", "host_exec"} <= set(definitions)
    assert definitions["host_exec"]["dangerous"] is True

    service = client.app.state.ipc.ops_agent_service
    executed = []

    class FakeTools:
        def catalog(self):
            return []

        def execute(self, name, arguments):
            executed.append((name, arguments))
            return {"ok": True, "privilege": "host-root", "stdout": "uid=0(root)"}

    service.tools = FakeTools()
    responses = iter(
        [
            json.dumps(
                {
                    "reply": "",
                    "workflow": None,
                    "tool_call": {
                        "name": "host_exec",
                        "arguments": {"command": "id", "timeout": 10},
                    },
                }
            ),
            json.dumps({"reply": "宿主机检查完成：uid=0(root)", "workflow": None, "tool_call": None}),
        ]
    )
    seen_messages = []

    class FakeAdapter:
        def chat(self, messages, **kwargs):
            seen_messages.append(list(messages))
            return next(responses)

    monkeypatch.setattr("backend.ops.service.make_adapter", lambda *args, **kwargs: FakeAdapter())
    response = client.post("/api/ops/chat", json={"message": "检查宿主机权限"})

    assert response.status_code == 200
    assert executed == [("host_exec", {"command": "id", "timeout": 10})]
    assert response.json()["tool_calls"] == [{"name": "host_exec", "project_id": None, "ok": True}]
    assert "TOOL_RESULT host_exec" in seen_messages[1][-1]["content"]
    assert "uid=0(root)" in response.json()["reply"]


def test_host_exec_uses_privileged_host_root_helper(client, monkeypatch):
    class FakeContainer:
        def wait(self, timeout=None):
            return {"StatusCode": 0}

        def logs(self, **kwargs):
            return (b"host-ok\n", b"")

        def remove(self, force=False):
            return None

    captured = {}

    class FakeContainers:
        def run(self, **kwargs):
            captured.update(kwargs)
            return FakeContainer()

    class FakeClient:
        containers = FakeContainers()

    class FakeDocker:
        @staticmethod
        def from_env():
            return FakeClient()

    monkeypatch.setattr("backend.ops.tools._load_docker_sdk", lambda: FakeDocker)
    result = OpsToolExecutor(client.app.state.ipc).host_exec("id")

    assert result["ok"] is True
    assert result["privilege"] == "host-root"
    assert captured["privileged"] is True
    assert captured["pid_mode"] == "host"
    assert captured["network_mode"] == "host"
    assert captured["volumes"] == {"/": {"bind": "/host", "mode": "rw"}}
    assert "chroot /host" in captured["command"][-1]


def test_confirmed_workflow_can_preview_import_and_submit(client, monkeypatch):
    token_value = "platform-super-secret"
    created = client.post(
        "/api/ops/workflows",
        json={"workflow": workflow_payload(), "secrets": {"platform_token": token_value}},
    )
    assert created.status_code == 201
    workflow_id = created.json()["id"]
    assert token_value not in created.text
    assert created.json()["spec"]["challenges"]["header_names"] == ["Authorization"]
    assert created.json()["spec"]["challenges"]["headers"][0]["secret_set"] is True

    assert client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": "yes"},
    ).status_code == 409
    confirmation = client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    )
    assert confirmation.status_code == 200
    execution_token = confirmation.json()["execution_token"]

    calls = []

    class FakeResponse:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self._content = content
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

        def json(self):
            return self._payload

        def iter_content(self, chunk_size):
            yield self._content

    challenge_payload = {
        "data": [
            {
                "id": "42",
                "name": "Ops Web",
                "category": "web",
                "description": "Imported by workflow",
                "files": ["task.txt"],
            }
        ]
    }

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/challenges"):
            return FakeResponse(payload=challenge_payload)
        if url.endswith("/assets/task.txt"):
            return FakeResponse(content=b"attachment")
        if url.endswith("/api/challenges/42/submit"):
            assert kwargs["json"] == {"answer": "flag{ops}"}
            return FakeResponse(payload={"success": True})
        raise AssertionError(f"unexpected workflow request: {method} {url}")

    monkeypatch.setattr("backend.ops.network._pinned_request", fake_request)
    base = f"/api/ops/workflows/{workflow_id}/execute"
    preview = client.post(
        base,
        json={"execution_token": execution_token, "operation": "preview"},
    )
    assert preview.status_code == 200
    assert preview.json()["challenges"][0]["attachment_count"] == 1
    assert "assets/task.txt" not in preview.text

    imported = client.post(
        base,
        json={
            "execution_token": execution_token,
            "operation": "import",
            "select": ["42"],
        },
    )
    assert imported.status_code == 200
    project_id = imported.json()["imported"][0]["project_id"]
    assert imported.json()["imported"][0]["created"] is True
    assert client.app.state.ipc.attachments_dir(project_id).joinpath("task.txt").read_bytes() == b"attachment"

    repeated = client.post(
        base,
        json={
            "execution_token": execution_token,
            "operation": "import",
            "select": ["42"],
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["imported"][0]["project_id"] == project_id
    assert repeated.json()["imported"][0]["created"] is False

    submitted = client.post(
        base,
        json={
            "execution_token": execution_token,
            "operation": "submit",
            "external_id": "42",
            "flag": "flag{ops}",
        },
    )
    assert submitted.status_code == 200
    assert submitted.json() == {
        "operation": "submit",
        "external_id": "42",
        "status_code": 200,
        "accepted": True,
    }
    assert "flag{ops}" not in submitted.text

    with client.app.state.ipc.db.connect() as connection:
        graph_store.set_flag(connection, project_id, "flag{ops}")
    submitted_from_project = client.post(
        base,
        json={
            "execution_token": execution_token,
            "operation": "submit",
            "project_id": project_id,
        },
    )
    assert submitted_from_project.status_code == 200
    assert submitted_from_project.json()["project_id"] == project_id
    assert "flag{ops}" not in submitted_from_project.text
    assert all(call[2]["headers"]["Authorization"] == f"Token {token_value}" for call in calls)


def test_private_network_requires_explicit_workflow_opt_in(client):
    payload = workflow_payload(private=False)
    payload["challenges"]["list_url"] = payload["challenges"]["list_url"].replace(
        "http://", "https://"
    )
    payload["challenges"]["attachment_base_url"] = payload["challenges"][
        "attachment_base_url"
    ].replace("http://", "https://")
    payload["submit"]["url"] = payload["submit"]["url"].replace("http://", "https://")
    payload["challenges"]["headers"] = []
    payload["submit"]["headers"] = []
    created = client.post("/api/ops/workflows", json={"workflow": payload})
    workflow_id = created.json()["id"]
    confirmation = client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    )
    assert confirmation.status_code == 400
    assert "non-public" in confirmation.text


def test_workflow_rejects_credentials_in_url_or_unstructured_json(client):
    query_secret = workflow_payload()
    query_secret["challenges"]["list_url"] += "?token=literal-secret"
    response = client.post("/api/ops/workflows", json={"workflow": query_secret})
    assert response.status_code == 400
    assert "credentials" in response.text

    body_secret = workflow_payload()
    body_secret["submit"]["json_template"]["api_key"] = "literal-secret"
    response = client.post("/api/ops/workflows", json={"workflow": body_secret})
    assert response.status_code == 400
    assert "structured secret placeholder" in response.text


def test_dns_rebinding_to_private_address_is_blocked_before_transport(monkeypatch):
    answers = ["93.184.216.34", "127.0.0.1"]

    def resolver(host, port, **kwargs):
        address = answers.pop(0)
        return [(2, 1, 6, "", (address, port))]

    called = False

    def transport(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport must not receive a rebound private address")

    monkeypatch.setattr("backend.ops.network._pinned_request", transport)
    http = WorkflowHttpClient(["https://example.test/api"], resolver=resolver)
    with pytest.raises(NetworkPolicyError, match="non-public"):
        http.get("https://example.test/api")
    assert called is False


def test_edit_or_revoke_invalidates_execution_capability(client, monkeypatch):
    created = client.post(
        "/api/ops/workflows",
        json={
            "workflow": workflow_payload(),
            "secrets": {"platform_token": "platform-super-secret"},
        },
    ).json()
    workflow_id = created["id"]
    execution_token = client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    ).json()["execution_token"]

    edited_payload = workflow_payload()
    edited_payload["name"] = "Edited workflow"
    edited = client.put(
        f"/api/ops/workflows/{workflow_id}",
        json={"workflow": edited_payload},
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "draft"
    denied = client.post(
        f"/api/ops/workflows/{workflow_id}/execute",
        json={"execution_token": execution_token, "operation": "preview"},
    )
    assert denied.status_code == 403


def test_existing_openai_decide_does_not_gain_chat_only_max_tokens(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"action":"done","reason":"ok"}'}}]}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_key="key",
            base_url="https://llm.invalid/v1",
            model="model",
        )
    )
    assert adapter.decide({"step": 1}).kind == "done"
    assert "max_tokens" not in captured
    assert "thinking" not in captured


def test_gpt_56_auto_uses_responses_structured_output(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"action":"bash","command":"id"}',
                            }
                        ],
                    },
                ],
                "usage": {
                    "output_tokens": 42,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_key="key",
            base_url="https://api.openai.com",
            model="gpt-5.6",
        )
    )

    action = adapter.decide({"step": 1})

    assert action.kind == "bash"
    assert calls[0][0] == "https://api.openai.com/v1/responses"
    body = calls[0][1]["json"]
    assert "input" in body and "messages" not in body
    assert body["max_output_tokens"] == 16384
    assert body["reasoning"] == {"effort": "none"}
    assert body["text"]["format"]["type"] == "json_schema"
    assert "temperature" not in body
    assert adapter._last_response_meta["surface"] == "responses"
    assert adapter._last_response_meta["reasoning_tokens"] == 3


def test_gpt_56_auto_falls_back_to_chat_and_caches_surface(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/responses"):
            return FakeResponse(404, {"error": {"message": "unknown endpoint"}})
        return FakeResponse(
            200,
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"action":"done","reason":"ok"}'}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"
    assert adapter.decide({"step": 2}).kind == "done"

    assert calls[0][0].endswith("/v1/responses")
    assert calls[1][0].endswith("/v1/chat/completions")
    assert calls[1][1]["max_completion_tokens"] == 16384
    assert calls[1][1]["reasoning_effort"] == "none"
    assert calls[2][0].endswith("/v1/chat/completions")
    assert sum(url.endswith("/responses") for url, _ in calls) == 1


def test_openai_structured_output_degrades_from_schema_to_json_mode(monkeypatch):
    requests_seen = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        requests_seen.append(kwargs["json"])
        if len(requests_seen) == 1:
            return FakeResponse(
                400,
                {"error": {"message": "response_format json_schema is not supported"}},
            )
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"action":"done","reason":"ok"}'}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"
    assert requests_seen[0]["response_format"]["type"] == "json_schema"
    assert requests_seen[1]["response_format"] == {"type": "json_object"}


def test_openai_does_not_retry_auth_errors(monkeypatch):
    calls = 0

    class FakeResponse:
        status_code = 401

        def raise_for_status(self):
            raise requests.HTTPError("HTTP 401", response=self)

        def json(self):
            return {"error": {"message": "invalid API key"}}

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_key="secret-never-log",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )

    with pytest.raises(NonRetryableProviderError) as caught:
        adapter.decide({"step": 1})
    assert calls == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 401


def test_deepseek_decide_uses_json_mode_and_repairs_invalid_output(monkeypatch):
    requests_seen = []
    contents = iter(
        [
            ("I should inspect the target first.", "stop"),
            ('{"action":"bash","command":"file ./target"}', "stop"),
        ]
    )

    class FakeResponse:
        def __init__(self, content, finish_reason):
            self.content = content
            self.finish_reason = finish_reason

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "finish_reason": self.finish_reason,
                        "message": {"content": self.content, "reasoning_content": "brief reasoning"},
                    }
                ]
            }

    def fake_post(url, **kwargs):
        requests_seen.append(kwargs["json"])
        return FakeResponse(*next(contents))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="deepseek",
            api_key="key",
            base_url="https://api.deepseek.invalid/v1",
            model="deepseek-chat",
        )
    )

    action = adapter.decide({"step": 1})

    assert action.kind == "bash"
    assert action.args["command"] == "file ./target"
    assert len(requests_seen) == 2
    assert all(body["response_format"] == {"type": "json_object"} for body in requests_seen)
    assert all(body["thinking"] == {"type": "disabled"} for body in requests_seen)
    assert requests_seen[0]["max_tokens"] == 4096
    assert requests_seen[0]["temperature"] == 0.0


def test_decision_output_error_preserves_safe_response_diagnostics(monkeypatch):
    calls = 0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "still not an action"},
                    }
                ]
            }

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="deepseek",
            api_key="key",
            base_url="https://api.deepseek.invalid/v1",
            model="deepseek-chat",
        )
    )

    with pytest.raises(DecisionOutputError) as caught:
        adapter.decide({"step": 1})

    assert calls == 2
    assert len(caught.value.attempts) == 2
    assert caught.value.attempts[-1]["response"]["finish_reason"] == "length"
    assert caught.value.attempts[-1]["preview"] == "still not an action"
    assert "finish_reason=length" in str(caught.value)


def test_existing_claude_decide_keeps_original_request_fields(monkeypatch):
    captured = {"json": {}, "headers": {}}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"text": '{"action":"done","reason":"ok"}'}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"].update(kwargs["json"])
        captured["headers"].update(kwargs["headers"])
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    adapter = ClaudeAdapter(
        LLMConfig(
            api_format="claudecode",
            api_key="key",
            base_url="https://llm.invalid",
            model="model",
        )
    )
    assert adapter.decide({"step": 1}).kind == "done"
    assert captured["json"]["max_tokens"] == 1024
    assert "temperature" not in captured["json"]
    assert captured["headers"]["x-api-key"] == "key"
    assert captured["url"] == "https://llm.invalid/v1/messages"


def test_anthropic_adapter_joins_text_blocks_and_ignores_thinking(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
                "stop_reason": "end_turn",
                "usage": {"output_tokens": 2},
            }

    monkeypatch.setattr("requests.post", lambda url, **kwargs: FakeResponse())
    adapter = ClaudeAdapter(
        LLMConfig(
            api_format="anthropic",
            api_key="key",
            base_url="https://anthropic.invalid/v1/messages",
            model="claude-model",
        )
    )

    assert adapter.chat([{"role": "user", "content": "hello"}]) == "first\nsecond"
    assert adapter._last_response_meta["reasoning_present"] is True
    assert adapter._last_response_meta["completion_tokens"] == 2
