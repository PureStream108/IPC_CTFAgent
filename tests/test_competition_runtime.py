from __future__ import annotations

import asyncio

import pytest
from types import SimpleNamespace

from backend.competition.conversation import ToolCall, Turn
from backend.competition.runtime import SessionRunner
from backend.members.base_member import BaseMember, DispatchResult, SolveResult


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


def test_session_runner_records_context_compression_failure():
    store = MemoryStore()
    runner = SessionRunner(
        store, Adapter([Turn([{"role": "assistant", "content": "done"}], [], "done")]),
        Executor(), assignment=assignment(), system="solve", tools=[], context_bytes=16_384,
    )
    with pytest.raises(RuntimeError, match="context exceeds"):
        runner.run("x" * 20_000)
    assert any(item["kind"] == "compression_failed" for item in store.items)


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
