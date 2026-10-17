from __future__ import annotations

import asyncio

from backend.agent.runtime import AgentRuntime
from backend.core.config import LLMConfig
from backend.members.adapters import MemberAction
from backend.members.member_tools import (
    MEMBER_TOOLS,
    action_payload,
    is_parallel_safe,
    is_terminal,
    tool_call_for_action,
)
from backend.members.mock_adapter import MockAdapter


class Writer:
    def __init__(self):
        self.items = []

    @property
    def session_id(self):
        return "s1"

    def append(self, event_key, kind, payload):
        item = {
            "sequence": len(self.items) + 1,
            "session_id": "s1",
            "event_key": event_key,
            "kind": kind,
            "payload": payload,
        }
        self.items.append(item)
        return item

    def events(self, after, limit):
        return [item for item in self.items if item["sequence"] > after][:limit]

    def save_checkpoint(self, checkpoint):
        return None


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments, *, idempotency_key):
        del idempotency_key
        self.calls.append((name, arguments))
        return {"status": "ok", "tool": name}


def _runtime(adapter, executor, writer):
    return AgentRuntime(
        writer, adapter, executor, system="solve", tools=list(MEMBER_TOOLS),
        parallel_safe=is_parallel_safe, is_terminal=is_terminal,
    )


def test_script_produces_one_native_tool_call_per_action():
    script = [
        {"action": "bash", "command": "echo recon", "thought": "look around"},
        {"action": "memory", "query": "web sqli"},
        {"action": "done", "reason": "finished"},
    ]
    adapter = MockAdapter(LLMConfig(api_format="mock"), script=script)
    executor = RecordingExecutor()
    writer = Writer()
    result = asyncio.run(_runtime(adapter, executor, writer).run_async("start"))

    assert result.status == "idle"
    expected = [tool_call_for_action(action) for action in script]
    assert [(name, args) for name, args in executor.calls] == [
        (name, arguments) for name, arguments in expected
    ]
    for (name, arguments), scripted in zip(executor.calls, script):
        action = MemberAction.from_obj(action_payload(name, arguments))
        assert action.kind == scripted["action"]
    calls = [item for item in writer.items if item["kind"] == "tool_call"]
    results = [item for item in writer.items if item["kind"] == "tool_result"]
    assert len(calls) == len(results) == len(script)
    assert [item["payload"]["name"] for item in calls] == [
        name for name, _ in executor.calls
    ]


def test_assistant_messages_carry_the_provider_tool_calls_shape():
    script = [{"action": "bash", "command": "id", "thought": "who am i"}]
    adapter = MockAdapter(LLMConfig(api_format="mock"), script=script)
    executor = RecordingExecutor()
    writer = Writer()
    asyncio.run(_runtime(adapter, executor, writer).run_async("start"))

    provider_events = [
        item for item in writer.items if item["kind"] == "provider_messages"
    ]
    messages = [
        message
        for event in provider_events
        for message in event["payload"]["messages"]
    ]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["role"] == "assistant"
    tool_calls = assistant["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "bash"
    assert tool_calls[0]["id"] == "mock-0"


def test_script_exhaustion_finishes_idle_without_inventing_actions():
    script = [{"action": "bash", "command": "echo once"}]
    adapter = MockAdapter(LLMConfig(api_format="mock"), script=script)
    executor = RecordingExecutor()
    writer = Writer()
    result = asyncio.run(_runtime(adapter, executor, writer).run_async("start"))

    assert result.status == "idle"
    assert [name for name, _ in executor.calls] == ["bash"]
    provider_events = [
        item for item in writer.items if item["kind"] == "provider_messages"
    ]
    last = provider_events[-1]["payload"]["messages"][-1]
    assert last["role"] == "assistant"
    assert not last.get("tool_calls")


def test_no_script_echoes_and_produces_no_tool_calls():
    adapter = MockAdapter(LLMConfig(api_format="mock"))
    executor = RecordingExecutor()
    writer = Writer()
    result = asyncio.run(_runtime(adapter, executor, writer).run_async("hello mock"))

    assert result.status == "idle"
    assert executor.calls == []
    assert "hello mock" in result.text


def test_legacy_decide_surface_is_retained_for_the_fallback_loop():
    """The legacy ``decide`` loop stays as the explicit fallback driver."""
    script = [{"action": "bash", "command": "id"}]
    adapter = MockAdapter(LLMConfig(api_format="mock"), script=script)
    action = adapter.decide({"step": 1})
    assert action.kind == "bash"
    assert action.args["command"] == "id"
    assert adapter._stream_script == script
    assert adapter.provider_session_id == "mock-session"
