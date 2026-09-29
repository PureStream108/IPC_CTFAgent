"""The shared action loop, independent of who owns the session.

These cases exercise ``AgentRuntime`` through an in-memory writer: no step
budget, durable repair of interrupted side effects, real loop detection,
truncation continuation, and a projection that keeps a compaction summary in
front of the retained tail.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.agent.context import compacted_history, summary_message
from backend.agent.runtime import AgentRuntime, SessionResult
from backend.competition.conversation import ToolCall, Turn, TurnTruncated


class MemoryWriter:
    """Append-only transcript with the same idempotency rule as the store."""

    def __init__(self, session_id: str = "s1") -> None:
        self._session_id = session_id
        self.items: list[dict] = []
        self.checkpoints: list[dict] = []
        self.usage: list[tuple] = []
        self.calibrations: list[float] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    def append(self, event_key, kind, payload):
        existing = next((item for item in self.items if item["event_key"] == event_key), None)
        if existing:
            return existing
        item = {
            "session_id": self._session_id,
            "sequence": len(self.items) + 1,
            "event_key": event_key,
            "kind": kind,
            "payload": payload,
        }
        self.items.append(item)
        return item

    def events(self, after=0, limit=500):
        return [item for item in self.items if item["sequence"] > after][:limit]

    def save_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)

    def record_usage(self, turn, status, usage=None):
        self.usage.append((turn, status, usage))

    def save_calibration(self, calibration):
        self.calibrations.append(calibration)


class Adapter:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def turn(self, messages, system, tools, emit, cancel):
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        emit(turn.text)
        return turn

    def tool_result(self, call, output):
        return {"role": "tool", "tool_call_id": call.id, "content": str(output)}


class Executor:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments, *, idempotency_key):
        self.calls.append((name, arguments, idempotency_key))
        return {"ok": True}


class AsyncExecutor(Executor):
    async def execute_async(self, name, arguments, *, idempotency_key):
        self.calls.append((name, arguments, idempotency_key))
        await asyncio.sleep(0)
        return {"ok": True, "async": True}


def runtime(writer, adapter, executor=None, **kwargs):
    return AgentRuntime(
        writer, adapter, executor or Executor(), system="solve", tools=[], **kwargs
    )


def test_native_messages_and_tool_results_are_persisted_in_order():
    writer = MemoryWriter()
    executor = Executor()
    adapter = Adapter([
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "c1"}]}],
            [ToolCall("c1", "shell", {"command": "id"})],
            "working",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ])
    result = runtime(writer, adapter, executor).run("start")

    assert result == SessionResult("idle", 2, "done")
    assert executor.calls == [("shell", {"command": "id"}, "s1:c1")]
    assert [item["kind"] for item in writer.items] == [
        "provider_messages", "provider_messages", "tool_call", "tool_result",
        "provider_messages",
    ]
    assert writer.checkpoints[-1]["status"] == "idle"


def test_there_is_no_step_budget_but_a_real_repeated_loop_is_detected():
    repeated = Turn(
        [{"role": "assistant", "tool_calls": [{"id": "call"}]}],
        [ToolCall("call", "shell", {"command": "pwd"})],
    )
    with pytest.raises(RuntimeError, match="identical tool batch"):
        runtime(
            MemoryWriter(), Adapter([repeated] * 4), repeated_batch_limit=3
        ).run("start")


def test_progress_over_many_turns_is_not_cut_off_by_default():
    """40 productive turns must run; only max_turns imposes a ceiling."""
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": f"c{index}"}]}],
            [ToolCall(f"c{index}", "shell", {"step": index})],
        )
        for index in range(40)
    ]
    turns.append(Turn([{"role": "assistant", "content": "done"}], [], "done"))
    result = runtime(MemoryWriter(), Adapter(turns)).run("start")
    assert result.status == "idle"
    assert result.turns == 41


def test_max_turns_stops_the_loop_only_when_configured():
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": f"c{index}"}]}],
            [ToolCall(f"c{index}", "shell", {"step": index})],
        )
        for index in range(5)
    ]
    result = runtime(MemoryWriter(), Adapter(turns), max_turns=2).run("start")
    assert result == SessionResult("turn_budget", 2, "")


def test_interrupted_side_effects_are_marked_unknown_after_a_restart():
    writer = MemoryWriter()
    call = ToolCall("crash-call", "shell", {"command": "touch /tmp/x"})

    class FailingExecutor(Executor):
        def execute(self, name, arguments, *, idempotency_key):
            self.calls.append((name, arguments, idempotency_key))
            raise RuntimeError("worker crashed after dispatch")

    first = runtime(
        writer,
        Adapter([Turn([{"role": "assistant", "tool_calls": [{"id": call.id}]}], [call])]),
        FailingExecutor(),
    )
    with pytest.raises(RuntimeError, match="crashed"):
        first.run("start")

    resumed = Adapter([Turn([{"role": "assistant", "content": "resume"}], [], "resume")])
    assert runtime(writer, resumed).run().status == "idle"

    # The side effect may already have happened, so it is reported as unknown
    # rather than silently retried.
    replayed = resumed.seen[0]
    assert any(
        message.get("role") == "tool" and "unknown" in message.get("content", "")
        for message in replayed
    )
    assert sum(item["kind"] == "tool_result" for item in writer.items) == 1


def test_dangling_provider_tool_calls_are_repaired_so_history_stays_valid():
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "start"}]},
    )
    writer.append(
        "provider:1", "provider_messages",
        {"messages": [{
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "member_action"}},
                {"id": "c2", "type": "function", "function": {"name": "member_action"}},
            ],
        }]},
    )
    writer.append("tool-call:c1", "tool_call", {"id": "c1", "name": "member_action", "arguments": {}})
    writer.append(
        "tool-result:c1", "tool_result",
        {"id": "c1", "message": {"role": "tool", "tool_call_id": "c1", "content": "{}"}},
    )

    adapter = Adapter([Turn([{"role": "assistant", "content": "late"}], [], "late")])
    assert runtime(writer, adapter).run().status == "idle"

    tool_messages = [m for m in adapter.seen[0] if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_messages} == {"c1", "c2"}
    assert "unknown" in str(tool_messages[-1])


def test_restored_history_does_not_re_add_the_bootstrap_prompt():
    writer = MemoryWriter()
    runtime(
        writer, Adapter([Turn([{"role": "assistant", "content": "pause"}], [], "pause")])
    ).run("original")
    runtime(
        writer, Adapter([Turn([{"role": "assistant", "content": "resume"}], [], "resume")])
    ).run()

    initial = [item for item in writer.items if item["event_key"] == "session:user:initial"]
    assert len(initial) == 1
    assert any(item["event_key"] == "provider:2" for item in writer.items)


def test_a_compaction_summary_leads_the_rebuilt_projection():
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "start"}]},
    )
    writer.append(
        "compact:4", "context_compacted",
        {
            "summary": "verified: /admin accepts a forged token; do not retry SQLi",
            "messages": [{"role": "user", "content": "recent tail"}],
        },
    )
    adapter = Adapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    assert runtime(writer, adapter).run().status == "idle"

    first = adapter.seen[0][0]
    assert "[compacted context summary]" in first["content"]
    assert "do not retry SQLi" in first["content"]
    assert adapter.seen[0][1] == {"role": "user", "content": "recent tail"}


def test_a_live_compaction_hands_the_summary_to_the_next_turn():
    writer = MemoryWriter()
    adapter = Adapter([Turn([{"role": "assistant", "content": "done"}], [], "done")])
    result = runtime(
        writer, adapter, context_bytes=16_384,
        compressor=lambda _m: (
            "verified: the flag lives in /flag.txt",
            [{"role": "user", "content": "tail"}],
        ),
    ).run("x" * 20_000)

    assert result.status == "idle"
    assert any(item["kind"] == "context_compacted" for item in writer.items)
    assert "verified: the flag lives in /flag.txt" in adapter.seen[0][0]["content"]


def test_compaction_without_a_compressor_degrades_instead_of_failing():
    """Compaction is an optimisation; losing it must not end the task."""
    writer = MemoryWriter()
    adapter = Adapter([Turn([{"role": "assistant", "content": "done"}], [], "done")])
    result = runtime(writer, adapter, context_bytes=16_384).run("x" * 20_000)

    assert result.status == "idle"
    degraded = [item for item in writer.items if item["kind"] == "error"]
    assert degraded and degraded[0]["payload"]["status"] == "no_compressor"
    # The transcript was still compacted, deterministically.
    assert any(item["kind"] == "context_compacted" for item in writer.items)


def test_a_failing_semantic_compressor_degrades_and_keeps_running():
    writer = MemoryWriter()

    def broken(_messages):
        raise RuntimeError("summarizer endpoint is down")

    adapter = Adapter([Turn([{"role": "assistant", "content": "done"}], [], "done")])
    result = runtime(
        writer, adapter, context_bytes=16_384, compressor=broken
    ).run("y" * 20_000)

    assert result.status == "idle"
    degraded = [item for item in writer.items if item["kind"] == "error"]
    assert degraded[0]["payload"]["status"] == "compressor_failed"
    assert "summarizer endpoint is down" in degraded[0]["payload"]["error"]


def test_compaction_triggers_on_the_token_budget_not_only_bytes():
    writer = MemoryWriter()
    adapter = Adapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    # Well under the byte ceiling, but over a small token budget.
    result = runtime(
        writer, adapter,
        context_bytes=10_000_000,
        context_token_limit=200,
        compaction_trigger_ratio=0.75,
        compressor=lambda _m: ({"verified_facts": []}, [{"role": "user", "content": "tail"}]),
    ).run("z" * 4_000)

    assert result.status == "idle"
    compacted = [item for item in writer.items if item["kind"] == "context_compacted"]
    assert compacted
    checkpoint = writer.checkpoints[0]
    assert checkpoint["reason"] == "token budget"
    assert checkpoint["limit"] == 200


def test_calibration_follows_the_real_prompt_token_count():
    writer = MemoryWriter()
    turn = Turn([{"role": "assistant", "content": "done"}], [], "done")
    # A provider that charges roughly twice the local estimate.
    turn.usage = {"prompt_tokens": 10_000, "output_tokens": 10}
    instance = runtime(writer, Adapter([turn]), context_token_limit=100_000)
    instance.run("hello world, this is a modest prompt")

    assert instance.calibration > 1.0
    assert writer.calibrations and writer.calibrations[-1] == instance.calibration


def test_a_truncated_turn_is_continued_instead_of_failing_the_session():
    writer = MemoryWriter()
    partial = Turn([{"role": "assistant", "content": "cut off mid-"}], [], "cut off mid-")

    class TruncatingAdapter(Adapter):
        def __init__(self):
            super().__init__([Turn([{"role": "assistant", "content": "finished"}], [], "finished")])
            self.raised = False

        def turn(self, messages, system, tools, emit, cancel):
            self.seen.append(list(messages))
            if not self.raised:
                self.raised = True
                raise TurnTruncated(partial, "max_tokens")
            return self.turns.pop(0)

    adapter = TruncatingAdapter()
    assert runtime(writer, adapter).run("start").status == "idle"

    truncated = [i for i in writer.items if i["payload"].get("truncated") == "max_tokens"]
    assert len(truncated) == 1
    assert truncated[0]["payload"]["messages"][0] == partial.messages[0]
    assert adapter.seen[1][-2] == partial.messages[0]
    assert "stopped at the provider output limit" in adapter.seen[1][-1]["content"]
    assert ("truncated" in [status for _turn, status, _usage in writer.usage])


def test_repeated_truncation_eventually_fails_the_turn():
    writer = MemoryWriter()

    class AlwaysTruncating(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            raise TurnTruncated(
                Turn([{"role": "assistant", "content": "partial"}], [], "partial"),
                "max_tokens",
            )

    with pytest.raises(RuntimeError, match="kept truncating"):
        runtime(writer, AlwaysTruncating([]), truncation_retry_limit=2).run("start")
    assert writer.checkpoints[-1]["status"] == "truncation_exhausted"


def test_turn_usage_is_reported_to_the_writer_for_calibration():
    writer = MemoryWriter()
    turn = Turn([{"role": "assistant", "content": "done"}], [], "done")
    turn.usage = {"prompt_tokens": 900, "output_tokens": 120}
    runtime(writer, Adapter([turn])).run("start")
    assert writer.usage == [(1, "completed", {"prompt_tokens": 900, "output_tokens": 120})]


def test_the_async_bridge_persists_the_same_contract():
    writer = MemoryWriter()
    executor = AsyncExecutor()
    result = asyncio.run(
        runtime(
            writer,
            Adapter([
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "async-call"}]}],
                    [ToolCall("async-call", "member_action", {"action": "done"})],
                    "working",
                ),
                Turn([{"role": "assistant", "content": "complete"}], [], "complete"),
            ]),
            executor,
        ).run_async("start")
    )
    assert result.status == "idle"
    assert executor.calls == [("member_action", {"action": "done"}, "s1:async-call")]
    assert [item["kind"] for item in writer.items] == [
        "provider_messages", "provider_messages", "tool_call", "tool_result",
        "provider_messages",
    ]


def test_call_state_reports_pending_and_done_from_durable_events():
    writer = MemoryWriter()
    instance = runtime(writer, Adapter([]))
    assert instance.call_state("c1") is None
    writer.append("tool-call:c1", "tool_call", {"id": "c1", "name": "shell", "arguments": {}})
    assert instance.call_state("c1") == "pending"
    writer.append("tool-result:c1", "tool_result", {"id": "c1", "message": {}})
    assert instance.call_state("c1") == "done"


def test_summary_projection_accepts_structured_and_legacy_summaries():
    structured = summary_message({"failed_paths": [{"approach": "sqli", "do_not_retry": True}]})
    assert "do_not_retry" in structured["content"]
    assert summary_message("plain text")["content"].endswith("plain text")
    assert summary_message("") is None
    assert summary_message({}) is None
    # An empty summary leaves the retained tail untouched.
    assert compacted_history({"summary": "", "messages": [{"role": "user", "content": "t"}]}) == [
        {"role": "user", "content": "t"}
    ]


def test_a_reused_tool_call_id_with_different_arguments_is_rejected():
    writer = MemoryWriter()
    writer.append("tool-call:c1", "tool_call", {"id": "c1", "name": "shell", "arguments": {"a": 1}})
    writer.append("tool-call:dup", "tool_call", {"id": "c1", "name": "shell", "arguments": {"a": 2}})
    with pytest.raises(RuntimeError, match="reused with different arguments"):
        runtime(writer, Adapter([])).run("start")
