from __future__ import annotations

import asyncio

import pytest

from backend.agent.context import compacted_history, summary_message
from backend.agent.runtime import (
    AgentRuntime,
    AgentSessionWriter,
    AssignmentSessionWriter,
    SessionResult,
)
from backend.agent.session import SessionConflict, SessionNeedsRebuild
from backend.competition.conversation import ToolCall, Turn, TurnTruncated


class MemoryWriter:
    """Append-only transcript with the same idempotency rule as the store."""

    def __init__(self, session_id: str = "s1", epoch: int = 1) -> None:
        self._session_id = session_id
        self.epoch = epoch
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
            if existing["kind"] != kind or existing["payload"] != payload:
                raise SessionConflict("event key reused with different content")
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

def test_tool_results_are_reattached_to_their_declaring_turn():
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
    writer.append(
        "tool-result:c1", "tool_result",
        {"id": "c1", "message": {"role": "tool", "tool_call_id": "c1", "content": "{}"}},
    )
    writer.append(
        "session:user:i001", "provider_messages",
        {"messages": [{"role": "user", "content": "New assigned intent i001"}]},
    )
    writer.append(
        "tool-result:c2", "tool_result",
        {"id": "c2", "message": {"role": "tool", "tool_call_id": "c2", "content": "{}"}},
    )

    adapter = Adapter([Turn([{"role": "assistant", "content": "resume"}], [], "resume")])
    assert runtime(writer, adapter).run().status == "idle"

    replayed = adapter.seen[0]
    assistant_at = next(
        index for index, message in enumerate(replayed)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert [
        message.get("tool_call_id") for message in replayed[assistant_at + 1:assistant_at + 3]
    ] == ["c1", "c2"]
    assert replayed[assistant_at + 3]["role"] == "user"


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


def test_a_follow_up_message_reaches_the_provider_instead_of_replaying_the_first():
    """A second operator message in a live session must be recorded.

    Without this, the loop rebuilds the old transcript, never sees the new
    question and answers the previous one again - the conversation silently
    stops responding to input.
    """
    writer = MemoryWriter()

    class Echo:
        """Mimics a provider: answers whatever the newest user message says."""

        def __init__(self):
            self.prompts: list[str] = []

        def turn(self, messages, system, tools, emit, cancel):
            latest = next(
                message["content"] for message in reversed(messages)
                if message.get("role") == "user"
            )
            self.prompts.append(latest)
            return Turn([{"role": "assistant", "content": f"re: {latest}"}], [], f"re: {latest}")

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    adapter = Echo()
    assert runtime(writer, adapter).run("first question").text == "re: first question"
    assert runtime(writer, adapter).run("second question").text == "re: second question"
    assert runtime(writer, adapter).run("third question").text == "re: third question"
    assert adapter.prompts == ["first question", "second question", "third question"]

    recorded = [
        message["content"]
        for item in writer.items
        if item["kind"] in ("provider_messages", "user_message")
        for message in item["payload"]["messages"]
        if message.get("role") == "user"
    ]
    assert recorded == ["first question", "second question", "third question"]


def test_resuming_without_new_input_does_not_duplicate_the_last_message():
    """A watchdog takeover or restart resumes the same session with no input."""
    writer = MemoryWriter()
    adapter = Adapter([
        Turn([{"role": "assistant", "content": "a"}], [], "a"),
        Turn([{"role": "assistant", "content": "b"}], [], "b"),
        Turn([{"role": "assistant", "content": "c"}], [], "c"),
    ])
    runtime(writer, adapter).run("only question")
    runtime(writer, adapter).run(None)
    runtime(writer, adapter).run("only question")

    users = [
        message
        for item in writer.items
        if item["kind"] in ("provider_messages", "user_message")
        for message in item["payload"]["messages"]
        if message.get("role") == "user"
    ]
    assert users == [{"role": "user", "content": "only question"}]


def test_a_recorded_user_message_event_is_projected_into_the_history():
    """``user_message`` is how Ops seeds pre-runtime history; it must project."""
    writer = MemoryWriter()
    writer.append(
        "session:bootstrap", "user_message",
        {"messages": [{"role": "user", "content": "history from before"}]},
    )
    adapter = Adapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    runtime(writer, adapter).run("now this")

    assert adapter.seen[0] == [
        {"role": "user", "content": "history from before"},
        {"role": "user", "content": "now this"},
    ]


def test_an_unset_token_limit_is_inferred_from_the_adapter_model():
    """The default config value is 0; it must not mean "no token budget"."""
    class Configured(Adapter):
        max_output_tokens = 32_768

        class config:
            model = "claude-opus-4-8"

    writer = MemoryWriter()
    adapter = Configured([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    instance = runtime(writer, adapter)
    assert instance.context_token_limit > 100_000

    class Unknown(Adapter):
        class config:
            model = "bespoke-internal-llm"

    assert runtime(
        MemoryWriter(), Unknown([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    ).context_token_limit == 0


class _Status400Error(Exception):
    """Stand-in for a strict provider rejecting an invalid tool-order turn."""

    def __init__(self, status_code: int = 400) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_continuation_message_uses_a_stable_epoch_scoped_key_not_a_position():
    """A resumed-session follow-up must key on (epoch, content), not length.

    A position key (`session:user:{len(events)}`) drifts the moment the event
    count changes, so a replay re-appends the same message or a new one reuses
    an old slot. The stable key keeps replay idempotent and distinct messages
    distinct, with the epoch rooting out cross-generation collisions.
    """
    writer = MemoryWriter(epoch=7)
    adapter = Adapter([
        Turn([{"role": "assistant", "content": "a"}], [], "a"),
        Turn([{"role": "assistant", "content": "b"}], [], "b"),
    ])
    runtime(writer, adapter).run("first")
    runtime(writer, adapter).run("second")

    follow_ups = [item for item in writer.items if item["kind"] == "user_message"]
    assert len(follow_ups) == 1
    key = follow_ups[0]["event_key"]
    assert key.startswith("session:user:continue:7:")
    import hashlib

    expected = (
        "session:user:continue:7:"
        + hashlib.sha256(b"second").hexdigest()[:16]
    )
    assert key == expected


def test_replaying_the_same_continuation_is_idempotent_under_the_stable_key():
    """Re-running the identical resume must not double the follow-up event."""
    writer = MemoryWriter(epoch=2)
    runtime(writer, Adapter([Turn([{"role": "assistant", "content": "x"}], [], "x")])).run("start")

    def resume():
        adapter = Adapter([Turn([{"role": "assistant", "content": "y"}], [], "y")])
        runtime(writer, adapter).run("please continue the work")

    resume()
    before = len([i for i in writer.items if i["kind"] == "user_message"])
    resume()
    resume()
    after = len([i for i in writer.items if i["kind"] == "user_message"])
    assert before == after == 1


def test_watchdog_resume_without_new_input_never_triggers_a_rebuild():
    """Resuming with no input must stay a plain replay, not a rebuild signal."""
    writer = MemoryWriter(epoch=3)
    adapter = Adapter([
        Turn([{"role": "assistant", "content": "a"}], [], "a"),
        Turn([{"role": "assistant", "content": "b"}], [], "b"),
    ])
    runtime(writer, adapter).run("only question")
    result = runtime(writer, adapter).run(None)
    assert result.status == "idle"
    assert not [i for i in writer.items if i["kind"] == "user_message"]


def test_async_tool_order_repair_continues_without_replaying_an_interrupted_call():
    writer = MemoryWriter()
    executor = AsyncExecutor()
    queued = [[{"role": "assistant", "tool_calls": [{
        "id": "interrupted-call", "function": {"name": "shell", "arguments": "{}"},
    }]}]]

    def deliver_after_bootstrap(_runtime):
        return queued.pop(0) if queued else []

    class RecoveringAdapter(Adapter):
        emits_reasoning = True

        def turn(self, messages, system, tools, emit, cancel, emit_reasoning):
            self.seen.append(list(messages))
            emit_reasoning("checking recovered history")
            if len(self.seen) == 1:
                raise _Status400Error()
            if len(self.seen) == 3:
                return Turn([{"role": "assistant", "content": "complete"}], [], "complete")
            return Turn(
                [{"role": "assistant", "tool_calls": [{"id": "recovery-check"}]}],
                [ToolCall("recovery-check", "shell", {"command": "pwd"})],
                "recovered",
            )

    adapter = RecoveringAdapter([])
    result = asyncio.run(runtime(
        writer, adapter, executor, before_turn=deliver_after_bootstrap,
        max_turns=2,
    ).run_async("continue"))

    assert result.status == "idle" and result.turns == 2
    assert len(adapter.seen) == 3
    assert any(message.get("tool_call_id") == "interrupted-call" for message in adapter.seen[1])
    assert executor.calls == [("shell", {"command": "pwd"}, "s1:recovery-check")]
    assert any(item["kind"] == "status" and item["payload"].get("status") == "tool_order_repaired" for item in writer.items)
    assert writer.epoch == 1


def test_an_invalid_tool_order_turn_is_repaired_in_place_before_a_retry():
    """A 400 whose repair changes the projection retries over the fixed history.

    The handler is exercised directly: bootstrap already pre-repairs durable
    poison, so the realistic path for a healable 400 reaching the loop is a
    projection that still carries a fenceable dangling call. ``_handle_turn_failure``
    must return the repaired projection (not raise) so the loop retries.
    """
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "go"}]},
    )
    inst = runtime(writer, Adapter([]))
    poisoned = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "shell"}}]},
    ]
    repaired = inst._handle_turn_failure(_Status400Error(), poisoned, turn_number=5, repaired_turn=-1)
    assert repaired != poisoned
    assert any(m.get("role") == "tool" for m in repaired)
    healed = [i for i in writer.items if i["kind"] == "status"
              and i["payload"].get("status") == "tool_order_repaired"]
    assert healed


def test_a_second_400_on_an_already_repaired_turn_escalates_to_rebuild():
    """One repair per turn: a persistent 400 after repair must rebuild, not loop."""
    writer = MemoryWriter()
    inst = runtime(writer, Adapter([]))
    clean = [{"role": "user", "content": "go"}]
    with pytest.raises(SessionNeedsRebuild) as caught:
        inst._handle_turn_failure(_Status400Error(), clean, turn_number=5, repaired_turn=5)
    assert caught.value.reason == "invalid_tool_order"


def test_a_400_that_repair_cannot_fix_escalates_to_a_rebuild_without_looping():
    """If repair changes nothing and the 400 persists, rebuild once - no loop."""
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "go"}]},
    )

    class AlwaysRejects:
        def __init__(self):
            self.calls = 0

        def turn(self, messages, system, tools, emit, cancel):
            self.calls += 1
            raise _Status400Error()

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    adapter = AlwaysRejects()
    with pytest.raises(SessionNeedsRebuild) as caught:
        runtime(writer, adapter).run(None)
    assert caught.value.reason == "invalid_tool_order"
    assert adapter.calls == 1


def test_a_non_400_provider_error_still_propagates_unchanged():
    """Transient 429/5xx must keep their existing retry path, not self-heal."""
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "go"}]},
    )

    class RateLimited:
        def turn(self, messages, system, tools, emit, cancel):
            raise _Status400Error(status_code=429)

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    with pytest.raises(_Status400Error):
        runtime(writer, RateLimited()).run(None)


def test_a_reused_continuation_key_with_different_content_needs_a_rebuild():
    """A durable collision under a reused key is classified as needs-rebuild."""
    writer = MemoryWriter(epoch=1)
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "go"}]},
    )
    import hashlib

    text = "resume please"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    writer.append(
        f"session:user:continue:1:{digest}", "user_message",
        {"messages": [{"role": "user", "content": "a DIFFERENT stored message"}]},
    )
    adapter = Adapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")])
    with pytest.raises(SessionNeedsRebuild) as caught:
        runtime(writer, adapter).run(text)
    assert caught.value.reason == "event_key_conflict"



def test_an_explicit_token_limit_overrides_the_inferred_window():
    class Configured(Adapter):
        class config:
            model = "claude-opus-4-8"

    instance = runtime(
        MemoryWriter(),
        Configured([Turn([{"role": "assistant", "content": "ok"}], [], "ok")]),
        context_token_limit=4_096,
    )
    assert instance.context_token_limit == 4_096


def test_calibration_follows_the_real_prompt_token_count():
    writer = MemoryWriter()
    turn = Turn([{"role": "assistant", "content": "done"}], [], "done")
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
    assert compacted_history({"summary": "", "messages": [{"role": "user", "content": "t"}]}) == [
        {"role": "user", "content": "t"}
    ]


def test_a_reused_tool_call_id_with_different_arguments_is_rejected():
    writer = MemoryWriter()
    writer.append("tool-call:c1", "tool_call", {"id": "c1", "name": "shell", "arguments": {"a": 1}})
    writer.append("tool-call:dup", "tool_call", {"id": "c1", "name": "shell", "arguments": {"a": 2}})
    with pytest.raises(RuntimeError, match="reused with different arguments"):
        runtime(writer, Adapter([])).run("start")


def test_async_truncation_continues_and_preserves_partial_evidence():
    class TruncatedAdapter(Adapter):
        def __init__(self):
            super().__init__([])
            self.calls = 0

        def turn(self, messages, system, tools, emit, cancel):
            self.calls += 1
            if self.calls == 1:
                raise TurnTruncated(Turn([{"role": "assistant", "content": "partial evidence"}], text="partial evidence"), "length")
            assert "partial evidence" in str(messages)
            return Turn([{"role": "assistant", "content": "continued"}], text="continued")

    writer = MemoryWriter()
    result = asyncio.run(runtime(writer, TruncatedAdapter()).run_async("start"))
    assert result.status == "idle" and result.text == "continued"
    assert writer.usage[0][1] == "truncated"


def test_async_repeated_tool_batches_are_bounded_with_distinct_call_ids():
    turns = [Turn([{"role": "assistant", "content": "check"}],
                  [ToolCall(f"call-{index}", "shell", {"command": "pwd"})]) for index in range(3)]
    writer = MemoryWriter()
    with pytest.raises(RuntimeError, match="identical tool batch"):
        asyncio.run(runtime(writer, Adapter(turns), repeated_batch_limit=3).run_async("start"))
    assert writer.items[-1]["kind"] == "loop_detected"


def test_durable_writer_adapters_forward_fences_and_calibration():
    class Store:
        def __init__(self):
            self.calls = []
            self.page = 0

        def append_event(self, *args, **kwargs):
            self.calls.append(("append", args, kwargs))
            return {"kind": args[2]}

        def events(self, session_id, *, after=0, limit=500):
            self.calls.append(("events", session_id, after, limit))
            if after == 0 and self.page == 0:
                self.page += 1
                return [{"sequence": index} for index in range(1, 501)]
            return [{"sequence": 501}] if after == 500 else []

        def save_checkpoint(self, *args):
            self.calls.append(("checkpoint", args))

        def record_compaction(self, *args, **kwargs):
            self.calls.append(("compaction", args, kwargs))

        def session(self, _session_id):
            return {"token_budget": {"old": 1}}

        def save_token_budget(self, *args):
            self.calls.append(("budget", args))

        def handoff(self, _session_id, new_owner, *, reason):
            self.calls.append(("handoff", new_owner, reason))
            return {"owner": new_owner, "epoch": 4}

        def record_turn(self, assignment, turn, status, *, usage=None):
            self.calls.append(("turn", assignment, turn, status, usage))

        def renew_assignment_writer(self, assignment):
            self.calls.append(("renew", assignment))
            return True

    store = Store()
    writer = AgentSessionWriter(store, "s1", "owner-a", 3)
    assert writer.session_id == "s1"
    writer.append("e", "status", {"ok": True})
    assert len(writer.events(0, 10)) == 500
    writer.save_checkpoint({"status": "ok"})
    store.page = 0
    writer.record_compaction({"facts": []}, token_estimate=12)
    writer.save_calibration(1.25)
    assert writer.handoff("owner-b", reason="resume") == {"owner": "owner-b", "epoch": 4}
    assert writer.owner == "owner-b" and writer.epoch == 4
    assert any(item[0] == "compaction" for item in store.calls)

    assignment = {"id": "a1", "session_id": "s1", "lease_owner": "owner", "epoch": 2}
    seat = AssignmentSessionWriter(store, assignment)
    assert seat.session_id == "s1"
    seat.append("e2", "status", {})
    seat.save_checkpoint({"status": "running"})
    seat.record_usage(2, "completed", {"prompt_tokens": 10})
    assert seat.renew() is True
    seat.save_calibration(0.9)
    assert any(item[0] == "turn" for item in store.calls)
