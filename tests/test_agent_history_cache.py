from __future__ import annotations

import pytest

from backend.agent.runtime import AgentRuntime
from backend.competition.conversation import ToolCall
from tests.test_agent_runtime import MemoryWriter


def _runtime(writer, **kwargs):
    return AgentRuntime(
        writer,
        None,
        None,
        system="system",
        tools=[],
        context_bytes=1_000_000,
        **kwargs,
    )


def test_history_only_reads_the_increment():
    writer = MemoryWriter()
    runtime = _runtime(writer)

    writer.append("session:user:initial", "provider_messages", {"messages": [{"role": "user", "content": "hi"}]})
    assert [m["content"] for m in runtime._history()] == ["hi"]
    assert runtime.history_events_read == 1

    writer.append("provider:1", "provider_messages", {"messages": [{"role": "assistant", "content": "step 1"}]})
    writer.append("tool-call:1", "tool_call", {"id": "c1", "name": "bash", "arguments": {}})
    writer.append("tool-result:1", "tool_result", {"id": "c1", "message": {"role": "tool", "content": "out"}})

    messages = runtime._history()
    assert [m["content"] for m in messages] == ["hi", "step 1", "out"]
    assert runtime.history_events_read == 3

    runtime._history()
    assert runtime.history_events_read == 0


def test_history_applies_a_compaction_event_arriving_as_an_increment():
    writer = MemoryWriter()
    runtime = _runtime(writer)
    writer.append("session:user:initial", "provider_messages", {"messages": [{"role": "user", "content": "hi"}]})
    runtime._history()

    compacted = {
        "summary": {"text": "summary"},
        "messages": [{"role": "user", "content": "summary"}, {"role": "user", "content": "recent"}],
    }
    writer.append("compact:1:1", "context_compacted", compacted)
    messages = runtime._history()
    assert "compacted context summary" in messages[0]["content"]
    assert messages[-1]["content"] == "recent"
    assert [m["content"] for m in messages[1:]] == ["summary", "recent"]
    assert runtime.history_events_read == 1


def test_call_state_uses_an_index_and_stays_current():
    writer = MemoryWriter()
    runtime = _runtime(writer)
    call = ToolCall(id="call-1", name="bash", arguments={})
    runtime.record_tool_call(call)
    assert runtime.call_state("call-1") == "pending"
    runtime.record_tool_result(call, {"role": "tool", "content": "ok"})
    assert runtime.call_state("call-1") == "done"
    assert runtime.call_state("unknown") is None


def _compaction_runtime(tokens, state_writer, ratio=0.85):
    writer = MemoryWriter()
    runtime = _runtime(
        writer,
        context_token_limit=1000,
        compaction_trigger_ratio=0.75,
        state_write_ratio=ratio,
        state_writer=state_writer,
    )
    messages = [{"role": "user", "content": "x" * 100}]
    runtime._needs_compaction = lambda _messages: (True, {"reason": "token budget", "tokens": tokens})
    return runtime, messages


def test_state_writer_fires_above_the_ratio():
    seen = []
    runtime, messages = _compaction_runtime(900, seen.append)
    runtime._compact(messages, turn=1)
    assert seen and seen[0]["reason"] == "context-ratio"
    assert seen[0]["ratio"] == pytest.approx(0.9)


def test_state_writer_is_silent_below_the_ratio():
    seen = []
    runtime, messages = _compaction_runtime(500, seen.append)
    runtime._compact(messages, turn=1)
    assert seen == []


def test_state_writer_does_not_repeat_within_a_cycle():
    seen = []
    runtime, _messages = _compaction_runtime(950, seen.append)
    runtime._maybe_write_state({"tokens": 950})
    runtime._maybe_write_state({"tokens": 950})
    assert len(seen) == 1
