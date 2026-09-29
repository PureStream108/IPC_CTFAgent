from __future__ import annotations

import asyncio

import pytest
from types import SimpleNamespace

from backend.competition.conversation import ToolCall, Turn, TurnTruncated
from backend.competition.runtime import SessionRunner
from backend.members.base_member import (
    BaseMember,
    DispatchResult,
    SolveResult,
)


class MemoryStore:
    def __init__(self):
        self.items = []
        self.checkpoints = []

    def append_event(self, session_id, event_key, kind, payload, **lease):
        existing = next((item for item in self.items if item["event_key"] == event_key), None)
        if existing:
            return existing
        item = {
            "session_id": session_id,
            "sequence": len(self.items) + 1,
            "event_key": event_key,
            "kind": kind,
            "payload": payload,
        }
        self.items.append(item)
        return item

    def events(self, session_id, after=0, limit=500):
        return [
            item for item in self.items
            if item["session_id"] == session_id and item["sequence"] > after
        ][:limit]

    def save_checkpoint(self, assignment, checkpoint):
        self.checkpoints.append(checkpoint)


class Adapter:
    def __init__(self, turns):
        self.turns = list(turns)

    def turn(self, messages, system, tools, emit, cancel):
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


def assignment():
    return {
        "id": "a1",
        "session_id": "s1",
        "lease_owner": "worker",
        "epoch": 1,
    }


def test_session_runner_persists_native_messages_and_tool_results():
    store = MemoryStore()
    executor = Executor()
    adapter = Adapter([
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "c1"}]}],
            [ToolCall("c1", "shell", {"command": "id"})],
            "working",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ])
    result = SessionRunner(
        store, adapter, executor, assignment=assignment(), system="solve",
        tools=[],
    ).run("start")

    assert result.status == "idle"
    assert result.turns == 2
    assert executor.calls == [("shell", {"command": "id"}, "s1:c1")]
    assert [item["kind"] for item in store.items] == [
        "provider_messages", "provider_messages", "tool_call", "tool_result",
        "provider_messages",
    ]
    assert store.checkpoints[-1]["status"] == "idle"


def test_session_runner_has_no_step_budget_but_detects_real_repeated_loop():
    repeated = Turn(
        [{"role": "assistant", "tool_calls": [{"id": "call"}]}],
        [ToolCall("call", "shell", {"command": "pwd"})],
    )
    runner = SessionRunner(
        MemoryStore(), Adapter([repeated] * 4), Executor(),
        assignment=assignment(), system="solve", tools=[], repeated_batch_limit=3,
    )
    with pytest.raises(RuntimeError, match="identical tool batch"):
        runner.run("start")


def test_conversation_adapter_tolerates_null_stream_fields():
    import threading

    from backend.competition.conversation import ConversationAdapter
    from backend.core.config import LLMConfig

    payloads = [
        '{"choices":[{"index":0,"delta":{"role":"assistant","content":"working","tool_calls":null},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
        '"function":{"name":"member_action","arguments":"{\\"action\\": \\"done\\"}"}}]},"finish_reason":"tool_calls"}]}',
    ]

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for payload in payloads:
                yield ("data: " + payload).encode()
                yield b""
            yield b"data: [DONE]"
            yield b""

    adapter = ConversationAdapter(
        LLMConfig(
            api_format="openai", api_key="k",
            base_url="https://opencode.ai/zen/go/v1", model="mimo-v2.6-flash",
        ),
        request=lambda *args, **kwargs: FakeResponse(),
    )
    turn = adapter.turn(
        [{"role": "user", "content": "go"}], "system", [],
        lambda _text: None, threading.Event(),
    )

    assert turn.text == "working"
    assert [(call.id, call.name, call.arguments) for call in turn.tools] == [
        ("call_1", "member_action", {"action": "done"})
    ]


def test_session_runner_default_context_budget_is_one_megabyte():
    runner = SessionRunner(
        MemoryStore(), Adapter([]), Executor(), assignment=assignment(),
        system="solve", tools=[],
    )
    assert runner.context_bytes == 1_000_000


def test_member_observation_cap_is_five_kilobytes():
    member = BaseMember("amber", SimpleNamespace(), SimpleNamespace())
    member._observe("x" * 9000)
    assert len(member.observations[-1]) == 5000


def test_deterministic_compaction_keeps_tool_pairing_and_shrinks_transcript():
    """The no-model fallback must still produce a transcript providers accept."""
    from backend.agent.compaction import deterministic_summary

    messages = [{"role": "user", "content": "start"}]
    for index in range(100):
        call_id = f"call-{index}"
        messages.append(
            {
                "role": "assistant",
                "content": "digging",
                "tool_calls": [
                    {"id": call_id, "type": "function", "function": {"name": "member_action"}}
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": "A" * 30_000}
        )
    summary, compacted = deterministic_summary(messages)

    assert len(compacted) < len(messages)
    assert compacted[0] == {"role": "user", "content": "start"}
    # It records which tools were already used, so the model knows the ground
    # it covered even without a semantic summary.
    assert "member_action" in summary["verified_facts"][0]["claim"]
    assert summary["open_questions"]
    # No tool result may survive without its assistant tool call, or the next
    # provider request is rejected.
    assistant_ids = {
        call["id"]
        for message in compacted
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    assert all(
        message["tool_call_id"] in assistant_ids
        for message in compacted
        if message.get("role") == "tool"
    )


def test_session_runner_repairs_dangling_provider_tool_calls():
    store = MemoryStore()
    store.append_event(
        "s1", "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "start"}]},
    )
    store.append_event(
        "s1", "provider:1", "provider_messages",
        {"messages": [{
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "member_action"}},
                {"id": "c2", "type": "function", "function": {"name": "member_action"}},
            ],
        }]},
    )
    store.append_event(
        "s1", "tool-call:c1", "tool_call",
        {"id": "c1", "name": "member_action", "arguments": {}},
    )
    store.append_event(
        "s1", "tool-result:c1", "tool_result",
        {"id": "c1", "message": {"role": "tool", "tool_call_id": "c1", "content": "{}"}},
    )

    captured: dict = {}

    class CapturingAdapter(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            captured["messages"] = list(messages)
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

    runner = SessionRunner(
        store, CapturingAdapter([Turn([{"role": "assistant", "content": "late"}], [], "late")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
    )
    result = runner.run()

    assert result.status == "idle"
    tool_messages = [
        message for message in captured["messages"] if message.get("role") == "tool"
    ]
    assert {message["tool_call_id"] for message in tool_messages} == {"c1", "c2"}
    assert "unknown" in str(tool_messages[-1])
    assert any(
        item["kind"] == "tool_result" and item["payload"]["id"] == "c2"
        for item in store.items
    )


def test_base_member_native_bridge_returns_observations_to_provider():
    captured: dict = {}

    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "obs-1"}]}],
                    [ToolCall("obs-1", "member_action", {"action": "bash", "command": "id"})],
                    "run id",
                ),
                Turn([{"role": "assistant", "content": "done"}], [], "done"),
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del system, tools, cancel
            captured["messages"] = list(messages)
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return True

        def _release(self, project_id, intent_id):
            return None

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "observation fixture"}

        def _conclude_with_description(self, project_id, intent_id, desc):
            return SolveResult(status="concluded", steps=0, fact_id="f001")

        async def _dispatch(self, *args, **kwargs):
            return DispatchResult(observation="$ id\nuid=0(root) gid=0(root)")

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=MemoryStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "intent", "web", True, None)
    )

    assert result.status == "done"
    tool_messages = [
        message for message in captured["messages"] if message.get("role") == "tool"
    ]
    assert any("uid=0(root)" in str(message.get("content")) for message in tool_messages)


def test_session_runner_restores_history_without_readding_initial_prompt():
    store = MemoryStore()
    first = SessionRunner(
        store, Adapter([Turn([{"role": "assistant", "content": "pause"}], [], "pause")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
    )
    first.run("original")
    second = SessionRunner(
        store, Adapter([Turn([{"role": "assistant", "content": "resume"}], [], "resume")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
    )
    second.run()
    initial = [item for item in store.items if item["event_key"] == "session:user:initial"]
    assert len(initial) == 1
    assert any(item["event_key"] == "provider:2" for item in store.items)


def test_session_runner_marks_interrupted_side_effect_unknown_after_restart():
    store = MemoryStore()
    first_call = ToolCall("crash-call", "shell", {"command": "touch /tmp/x"})

    class FailingExecutor(Executor):
        def execute(self, name, arguments, *, idempotency_key):
            self.calls.append((name, arguments, idempotency_key))
            raise RuntimeError("worker crashed after dispatch")

    first = SessionRunner(
        store,
        Adapter([Turn([{"role": "assistant", "tool_calls": [{"id": first_call.id}]}], [first_call])]),
        FailingExecutor(), assignment=assignment(), system="solve", tools=[],
    )
    with pytest.raises(RuntimeError, match="crashed"):
        first.run("start")

    class InspectingAdapter(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            assert any(
                item.get("role") == "tool" and "unknown" in item.get("content", "")
                for item in messages if isinstance(item, dict)
            )
            return Turn([{"role": "assistant", "content": "resume"}], [], "resume")

    second = SessionRunner(
        store, InspectingAdapter([]), Executor(), assignment=assignment(),
        system="solve", tools=[],
    )
    result = second.run()
    assert result.status == "idle"
    assert sum(item["kind"] == "tool_result" for item in store.items) == 1
    assert store.checkpoints[-1]["status"] == "idle"


def test_session_runner_degrades_when_compaction_is_unavailable():
    """Context pressure must not end a task that is still making progress."""
    store = MemoryStore()
    runner = SessionRunner(
        store, Adapter([Turn([{"role": "assistant", "content": "done"}], [], "done")]),
        Executor(), assignment=assignment(), system="solve", tools=[], context_bytes=16_384,
    )
    assert runner.run("x" * 20_000).status == "idle"
    degraded = [item for item in store.items if item["kind"] == "error"]
    assert degraded and degraded[0]["payload"]["status"] == "no_compressor"


def test_restored_history_injects_the_stored_compaction_summary():
    store = MemoryStore()
    store.append_event(
        "s1", "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "start"}]},
    )
    store.append_event(
        "s1", "compact:4", "context_compacted",
        {
            "summary": "verified: /admin accepts a forged token; do not retry SQLi",
            "messages": [{"role": "user", "content": "recent tail"}],
        },
    )
    captured: dict = {}

    class CapturingAdapter(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            captured["messages"] = list(messages)
            return self.turns.pop(0)

    runner = SessionRunner(
        store, CapturingAdapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
    )
    assert runner.run().status == "idle"

    first = captured["messages"][0]
    assert first["role"] == "user"
    assert "[compacted context summary]" in first["content"]
    assert "do not retry SQLi" in first["content"]
    assert captured["messages"][1] == {"role": "user", "content": "recent tail"}


def test_live_compaction_hands_the_summary_to_the_next_turn():
    store = MemoryStore()
    captured: list[list[dict]] = []

    class CapturingAdapter(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            captured.append(list(messages))
            return self.turns.pop(0)

    runner = SessionRunner(
        store,
        CapturingAdapter([Turn([{"role": "assistant", "content": "done"}], [], "done")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
        context_bytes=16_384,
        compressor=lambda _messages: (
            "verified: the flag lives in /flag.txt",
            [{"role": "user", "content": "tail"}],
        ),
    )
    assert runner.run("x" * 20_000).status == "idle"

    assert any(item["kind"] == "context_compacted" for item in store.items)
    assert "verified: the flag lives in /flag.txt" in captured[0][0]["content"]


def test_history_without_a_summary_is_left_unchanged():
    store = MemoryStore()
    store.append_event(
        "s1", "compact:2", "context_compacted",
        {"summary": "", "messages": [{"role": "user", "content": "tail"}]},
    )
    captured: dict = {}

    class CapturingAdapter(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            captured["messages"] = list(messages)
            return self.turns.pop(0)

    SessionRunner(
        store, CapturingAdapter([Turn([{"role": "assistant", "content": "ok"}], [], "ok")]),
        Executor(), assignment=assignment(), system="solve", tools=[],
    ).run()
    assert captured["messages"] == [{"role": "user", "content": "tail"}]


def test_truncated_turn_is_continued_instead_of_failing_the_session():
    store = MemoryStore()
    partial = Turn([{"role": "assistant", "content": "I was cut off mid-"}], [], "I was cut off mid-")
    captured: list[list[dict]] = []

    class TruncatingAdapter(Adapter):
        def __init__(self):
            super().__init__([Turn([{"role": "assistant", "content": "finished"}], [], "finished")])
            self.raised = False

        def turn(self, messages, system, tools, emit, cancel):
            captured.append(list(messages))
            if not self.raised:
                self.raised = True
                raise TurnTruncated(partial, "max_tokens")
            return self.turns.pop(0)

    result = SessionRunner(
        store, TruncatingAdapter(), Executor(), assignment=assignment(),
        system="solve", tools=[],
    ).run("start")

    assert result.status == "idle"
    # The partial assistant message and the continuation directive are durable.
    truncated = [
        item for item in store.items
        if item["payload"].get("truncated") == "max_tokens"
    ]
    assert len(truncated) == 1
    assert truncated[0]["payload"]["messages"][0] == partial.messages[0]
    # The retry sees the partial turn plus an instruction to continue.
    assert captured[1][-2] == partial.messages[0]
    assert "stopped at the provider output limit" in captured[1][-1]["content"]


def test_repeated_truncation_eventually_fails_the_turn():
    store = MemoryStore()

    class AlwaysTruncating(Adapter):
        def turn(self, messages, system, tools, emit, cancel):
            raise TurnTruncated(
                Turn([{"role": "assistant", "content": "partial"}], [], "partial"),
                "max_tokens",
            )

    runner = SessionRunner(
        store, AlwaysTruncating([]), Executor(), assignment=assignment(),
        system="solve", tools=[], truncation_retry_limit=2,
    )
    with pytest.raises(RuntimeError, match="kept truncating"):
        runner.run("start")
    assert store.checkpoints[-1]["status"] == "truncation_exhausted"


def test_anthropic_output_limit_yields_a_continuable_partial_turn():
    import threading

    from backend.competition.conversation import ConversationAdapter
    from backend.core.config import LLMConfig

    payloads = [
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"analysing"}}',
        '{"type":"content_block_start","index":1,"content_block":'
        '{"type":"tool_use","id":"cut","name":"member_action"}}',
        '{"type":"content_block_delta","index":1,"delta":'
        '{"type":"input_json_delta","partial_json":"{\\"action\\": \\"ba"}}',
        '{"type":"message_delta","delta":{"stop_reason":"max_tokens"}}',
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for payload in payloads:
                yield ("data: " + payload).encode()
                yield b""

    adapter = ConversationAdapter(
        LLMConfig(api_format="anthropic", api_key="k", base_url="https://api.anthropic.com", model="m"),
        request=lambda *args, **kwargs: FakeResponse(),
    )
    with pytest.raises(TurnTruncated) as caught:
        adapter.turn([{"role": "user", "content": "go"}], "system", [], lambda _t: None, threading.Event())

    turn = caught.value.turn
    assert caught.value.reason == "max_tokens"
    # The half-streamed tool_use block is dropped: an unanswered tool call
    # would make the next request invalid.
    assert turn.tools == []
    content = turn.messages[0]["content"]
    assert [block["type"] for block in content] == ["text"]
    assert content[0]["text"] == "analysing"


def test_chat_completions_length_finish_is_truncation_not_failure():
    import threading

    from backend.competition.conversation import ConversationAdapter
    from backend.core.config import LLMConfig

    payloads = [
        '{"choices":[{"index":0,"delta":{"content":"partial answer"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}',
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            for payload in payloads:
                yield ("data: " + payload).encode()
                yield b""
            yield b"data: [DONE]"
            yield b""

    adapter = ConversationAdapter(
        LLMConfig(api_format="openai", api_key="k", base_url="https://example.invalid/v1", model="m"),
        request=lambda *args, **kwargs: FakeResponse(),
    )
    with pytest.raises(TurnTruncated) as caught:
        adapter.turn([{"role": "user", "content": "go"}], "system", [], lambda _t: None, threading.Event())

    assert caught.value.reason == "length"
    assert caught.value.turn.messages[0]["content"] == "partial answer"


def test_anthropic_refusal_still_fails_the_turn():
    import threading

    from backend.competition.conversation import ConversationAdapter
    from backend.core.config import LLMConfig

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield b'data: {"type":"message_delta","delta":{"stop_reason":"refusal"}}'
            yield b""

    adapter = ConversationAdapter(
        LLMConfig(api_format="anthropic", api_key="k", base_url="https://api.anthropic.com", model="m"),
        request=lambda *args, **kwargs: FakeResponse(),
    )
    with pytest.raises(RuntimeError, match="refused"):
        adapter.turn([{"role": "user", "content": "go"}], "system", [], lambda _t: None, threading.Event())


def test_session_runner_async_bridge_persists_the_same_native_contract():
    store = MemoryStore()
    executor = AsyncExecutor()
    result = asyncio.run(
        SessionRunner(
            store,
            Adapter([
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "async-call"}]}],
                    [ToolCall("async-call", "member_action", {"action": "done"})],
                    "working",
                ),
                Turn([{"role": "assistant", "content": "complete"}], [], "complete"),
            ]),
            executor,
            assignment=assignment(),
            system="solve",
            tools=[{"name": "member_action"}],
        ).run_async("start")
    )
    assert result.status == "idle"
    assert executor.calls == [("member_action", {"action": "done"}, "s1:async-call")]
    assert [item["kind"] for item in store.items] == [
        "provider_messages", "provider_messages", "tool_call", "tool_result",
        "provider_messages",
    ]


def test_base_member_native_bridge_routes_provider_action_through_dispatch():
    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "native-1"}]}],
                    [ToolCall("native-1", "member_action", {"action": "done", "reason": "finished"})],
                    "finish",
                )
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del messages, system, tools, cancel
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return True

        def _release(self, project_id, intent_id):
            return None

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "native fixture"}

        async def _dispatch(self, *args, **kwargs):
            return DispatchResult(result=SolveResult(status="done", steps=args[4]))

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=MemoryStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session(
            "project", "intent", "web", True, None
        )
    )
    assert result.status == "done"
    assert result.steps == 1
    assert any(item["kind"] == "tool_call" for item in deps.competition_store.items)


def test_base_member_native_idle_concludes_the_open_intent():
    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn([{"role": "assistant", "content": "task complete"}], [], "done")
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del messages, system, tools, cancel
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.concluded = None
            self.released = False

        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return True

        def _release(self, project_id, intent_id):
            self.released = True

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "native fixture"}

        def _conclude_with_description(self, project_id, intent_id, desc):
            self.concluded = (project_id, intent_id, desc)
            return SolveResult(status="concluded", steps=0, fact_id="f001")

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=MemoryStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "intent", "web", True, None)
    )

    assert result.status == "done"
    assert member.concluded == (
        "project",
        "intent",
        "member reported no further actions for this intent",
    )
    assert member.released is True


def test_base_member_native_continuation_appends_a_fresh_instruction():
    captured: dict = {}

    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn([{"role": "assistant", "content": "done"}], [], "done")
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del system, tools, cancel
            captured["messages"] = list(messages)
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return True

        def _release(self, project_id, intent_id):
            return None

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "continuation fixture"}

        def _conclude_with_description(self, project_id, intent_id, desc):
            return SolveResult(status="concluded", steps=0, fact_id="f001")

    store = MemoryStore()
    deps = SimpleNamespace(
        competition_native=True,
        competition_store=store,
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "i007", "web", False, None)
    )

    assert result.status == "done"
    user_texts = [
        str(message.get("content"))
        for message in captured["messages"]
        if message.get("role") == "user"
    ]
    assert any("New assigned intent i007" in text for text in user_texts)
    assert any(
        item["event_key"] == "session:user:i007" for item in store.items
    )


def test_base_member_native_continuation_tolerates_stored_conflict():
    from backend.competition.store import CompetitionConflict

    class ConflictingStore(MemoryStore):
        def append_event(self, session_id, event_key, kind, payload, **lease):
            if event_key == "session:user:i009":
                raise CompetitionConflict("event key reused with different content")
            return super().append_event(session_id, event_key, kind, payload, **lease)

    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn([{"role": "assistant", "content": "done"}], [], "done")
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del messages, system, tools, cancel
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return True

        def _release(self, project_id, intent_id):
            return None

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "conflict fixture"}

        def _conclude_with_description(self, project_id, intent_id, desc):
            return SolveResult(status="concluded", steps=0, fact_id="f001")

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=ConflictingStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "i009", "web", False, None)
    )

    assert result.status == "done"


def test_base_member_native_bridge_releases_lease_after_terminal_multi_turn_action():
    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        def __init__(self):
            self.config = SimpleNamespace(api_format="openai")
            self.turns = [
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "native-report"}]}],
                    [ToolCall("native-report", "member_action", {
                        "action": "report", "progress": "recon complete",
                    })],
                    "share recon",
                ),
                Turn(
                    [{"role": "assistant", "tool_calls": [{"id": "native-flag"}]}],
                    [ToolCall("native-flag", "member_action", {
                        "action": "flag", "flag": "flag{fixture}",
                        "description": "verified fixture evidence",
                    })],
                    "submit verified flag",
                ),
            ]

        def turn(self, messages, system, tools, emit, cancel):
            del messages, system, tools, cancel
            turn = self.turns.pop(0)
            emit(turn.text)
            return turn

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.heartbeats = []
            self.releases = []
            self.dispatched = []

        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            self.heartbeats.append((project_id, intent_id))
            return True

        def _release(self, project_id, intent_id):
            self.releases.append((project_id, intent_id))

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "native multi-turn fixture"}

        async def _dispatch(self, project_id, intent_id, category, action, step, mcp_session, **kwargs):
            del project_id, intent_id, category, mcp_session, kwargs
            self.dispatched.append(action.kind)
            if action.kind == "flag":
                return DispatchResult(
                    result=SolveResult(status="flag", steps=step, flag="flag{fixture}"),
                    graph_action="flag",
                )
            return DispatchResult(graph_action="report")

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=MemoryStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "intent", "web", True, None)
    )

    assert result.status == "flag"
    assert result.steps == 2
    assert member.dispatched == ["report", "flag"]
    assert member.heartbeats == [("project", "intent"), ("project", "intent")]
    assert member.releases == [("project", "intent")]
    assert sum(item["kind"] == "tool_call" for item in deps.competition_store.items) == 2


def test_base_member_native_bridge_releases_lease_when_heartbeat_is_fenced():
    class Logger:
        def project(self, *args, **kwargs):
            return None

        def llm(self, *args, **kwargs):
            return None

    class ProviderStream:
        config = SimpleNamespace(api_format="openai")

        def turn(self, messages, system, tools, emit, cancel):
            del messages, system, tools, cancel
            return Turn(
                [{"role": "assistant", "tool_calls": [{"id": "stale-call"}]}],
                [ToolCall("stale-call", "member_action", {"action": "report"})],
                "stale",
            )

        def tool_result(self, call, output):
            return {"role": "tool", "tool_call_id": call.id, "content": str(output)}

    class Member(BaseMember):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.releases = []
            self.dispatch_count = 0

        def _claim(self, project_id, intent_id):
            return True

        def _heartbeat(self, project_id, intent_id):
            return False

        def _release(self, project_id, intent_id):
            self.releases.append((project_id, intent_id))

        def _seed_tool_inventory(self, project_id):
            return None

        def _prime_tool_context(self, project_id, intent_id, category):
            return None

        def _build_context(self, *args):
            return {"challenge": "fenced fixture"}

        async def _dispatch(self, *args, **kwargs):
            self.dispatch_count += 1
            return DispatchResult()

    deps = SimpleNamespace(
        competition_native=True,
        competition_store=MemoryStore(),
        competition_assignment=assignment(),
        competition_conversation_factory=lambda _config: ProviderStream(),
        logger=Logger(),
    )
    member = Member("amber", ProviderStream(), deps)
    result = asyncio.run(
        member._solve_with_native_session("project", "intent", "web", True, None)
    )

    assert result.status == "stalled"
    assert result.error == "intent lease lost"
    assert member.dispatch_count == 0
    assert member.releases == [("project", "intent")]
