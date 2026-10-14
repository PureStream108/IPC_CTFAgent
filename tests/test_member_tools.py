"""One tool per Member action, and concurrency within a turn (P4b).

Two invariants are the point of these tests:

* A terminal action stops the rest of its batch. Running a side effect after
  the call that submitted a flag or concluded the intent would apply changes
  past the decision that ended the task, and the loop's dangling-call repair
  assumes nothing lands after that point.
* Parallel execution keeps ``tool-call:{id}`` and ``tool-result:{id}`` paired
  and in the provider's order. The transcript has to match the ``tool_calls``
  array regardless of which tool finished first.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.agent.runtime import AgentRuntime
from backend.competition.conversation import ToolCall, Turn
from backend.members.adapters import ACTION_KINDS, MemberAction
from backend.members.member_tools import (
    MEMBER_TOOL_NAMES,
    MEMBER_TOOLS,
    TOOL_ACTIONS,
    action_payload,
    is_parallel_safe,
    is_terminal,
)


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


class Adapter:
    def __init__(self, turns):
        self.turns = list(turns)

    def turn(self, messages, system, tools, emit, cancel):
        turn = self.turns.pop(0)
        emit(turn.text)
        return turn

    def tool_result(self, call, output):
        return {"role": "tool", "tool_call_id": call.id, "content": str(output)}


class RecordingExecutor:
    """Tracks overlap so concurrency is observed, not assumed."""

    def __init__(self, delays=None):
        self.delays = delays or {}
        self.started = []
        self.finished = []
        self.peak_concurrency = 0
        self._active = 0

    async def execute_async(self, name, arguments, *, idempotency_key):
        del idempotency_key
        self.started.append(name)
        self._active += 1
        self.peak_concurrency = max(self.peak_concurrency, self._active)
        await asyncio.sleep(self.delays.get(name, 0))
        self._active -= 1
        self.finished.append(name)
        return {"ok": True, "tool": name}


def runtime(turns, executor, **kwargs):
    return AgentRuntime(
        Writer(), Adapter(turns), executor, system="solve", tools=[],
        parallel_safe=is_parallel_safe, is_terminal=is_terminal, **kwargs,
    )


def test_every_action_kind_has_exactly_one_tool():
    """The split must not quietly drop a capability."""
    assert set(TOOL_ACTIONS.values()) == set(ACTION_KINDS)
    assert len(MEMBER_TOOLS) == len(MEMBER_TOOL_NAMES) == len(set(MEMBER_TOOL_NAMES))
    for tool in MEMBER_TOOLS:
        assert tool["name"] in TOOL_ACTIONS
        assert tool["parameters"]["type"] == "object"


def test_each_tool_payload_builds_a_valid_member_action():
    """The dispatcher is still keyed on the legacy kinds, so the map must hold."""
    samples = {
        "bash": {"command": "id"},
        "install": {"packages": ["jq"]},
        "mcp_tool": {"server": "web", "tool": "fetch", "args": {"url": "http://x"}},
        "memory_search": {"query": "sqli"},
        "tool_search": {"query": "pyjail"},
        "report": {"progress": "stuck", "difficulty": "high"},
        "declare_intent": {"description": "probe the admin panel"},
        "conclude": {"description": "confirmed LFI"},
        "submit_flag": {"flag": "flag{x}", "description": "derived from LFI"},
        "give_up": {"reason": "exhausted"},
        "job_start": {"command": "hashcat -a0 hash wordlist"},
        "job_status": {"job_id": "j1"},
        "job_logs": {"job_id": "j1", "stream": "stdout"},
        "job_cancel": {"job_id": "j1"},
    }
    assert set(samples) == set(MEMBER_TOOL_NAMES)
    for name, arguments in samples.items():
        action = MemberAction.from_obj(action_payload(name, arguments))
        assert action.kind == TOOL_ACTIONS[name]


def test_an_unknown_tool_name_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="unsupported provider tool"):
        action_payload("rm_rf", {"command": "rm -rf /"})


def test_nested_arguments_are_still_unwrapped():
    """Models trained on the single-tool shape keep nesting under ``args``."""
    action = MemberAction.from_obj(action_payload("bash", {"args": {"command": "ls"}}))
    assert action.kind == "bash"
    assert action.args["command"] == "ls"


def test_mcp_tool_keeps_its_own_args_object():
    """``mcp_tool`` genuinely has an ``args`` field; unwrapping it would lose it."""
    action = MemberAction.from_obj(
        action_payload("mcp_tool", {"server": "web", "tool": "fetch",
                                    "args": {"url": "http://x"}})
    )
    assert action.args["args"] == {"url": "http://x"}


def test_side_effecting_tools_are_never_parallel_safe():
    for name in ("bash", "install", "mcp_tool", "job_start", "job_cancel"):
        assert is_parallel_safe(name) is False, name
    for name in ("memory_search", "tool_search", "job_status", "job_logs"):
        assert is_parallel_safe(name) is True, name
    for name in ("submit_flag", "conclude", "give_up"):
        assert is_terminal(name) is True, name


def test_adjacent_read_only_calls_run_concurrently():
    executor = RecordingExecutor(
        delays={"memory_search": 0.05, "tool_search": 0.05}
    )
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}],
            [
                ToolCall("a", "memory_search", {"query": "one"}),
                ToolCall("b", "tool_search", {"query": "two"}),
            ],
            "reads",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    asyncio.run(runner.run_async("start"))
    assert executor.peak_concurrency == 2


def test_a_write_between_reads_is_not_reordered():
    """Grouping only adjacent reads.

    Hoisting a read across a write would let the model observe state from
    before a change it had already requested.
    """
    executor = RecordingExecutor()
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}],
            [
                ToolCall("a", "memory_search", {"query": "before"}),
                ToolCall("b", "bash", {"command": "rm notes.txt"}),
                ToolCall("c", "memory_search", {"query": "after"}),
            ],
            "mixed",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    asyncio.run(runner.run_async("start"))
    assert executor.started == ["memory_search", "bash", "memory_search"]
    assert executor.peak_concurrency == 1


def test_a_terminal_action_stops_the_rest_of_its_batch():
    """Side effects must not land after the decision that ended the task."""
    executor = RecordingExecutor()
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}],
            [
                ToolCall("a", "submit_flag", {"flag": "flag{x}", "description": "e"}),
                ToolCall("b", "bash", {"command": "rm -rf evidence"}),
            ],
            "terminal",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    asyncio.run(runner.run_async("start"))
    assert executor.started == ["submit_flag"]
    keys = [item["event_key"] for item in runner.writer.items]
    assert "tool-call:b" not in keys
    assert "tool-result:b" not in keys


def test_terminal_action_also_stops_the_batch_in_the_sync_loop():
    class SyncExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments, *, idempotency_key):
            del idempotency_key
            self.calls.append(name)
            return {"ok": True}

    executor = SyncExecutor()
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}],
            [
                ToolCall("a", "conclude", {"description": "done"}),
                ToolCall("b", "bash", {"command": "echo late"}),
            ],
            "terminal",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    runner.run("start")
    assert executor.calls == ["conclude"]


def test_concurrent_results_stay_paired_and_in_provider_order():
    """The transcript must match the tool_calls array, not completion order.

    This caught a real defect: appending each call/result pair from inside its
    own coroutine ordered the event log by completion, so a fast read landed
    ahead of a slow one the provider had listed first. History is rebuilt from
    that log, so the rebuilt array no longer lined up with its results.
    """
    executor = RecordingExecutor(
        delays={"memory_search": 0.08, "tool_search": 0.0}
    )
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "slow"}, {"id": "fast"}]}],
            [
                ToolCall("slow", "memory_search", {"query": "slow"}),
                ToolCall("fast", "tool_search", {"query": "fast"}),
            ],
            "reads",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    asyncio.run(runner.run_async("start"))

    assert executor.finished == ["tool_search", "memory_search"]
    calls = [
        item["payload"]["id"] for item in runner.writer.items
        if item["kind"] == "tool_call"
    ]
    results = [
        item["payload"]["id"] for item in runner.writer.items
        if item["kind"] == "tool_result"
    ]
    assert calls == ["slow", "fast"]
    assert results == ["slow", "fast"]
    assert len(results) == len(set(results))


def test_sequential_execution_is_the_default_without_the_predicates():
    """Existing callers that pass no predicates keep serial behaviour."""
    executor = RecordingExecutor(delays={"memory_search": 0.05, "tool_search": 0.05})
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}],
            [
                ToolCall("a", "memory_search", {"query": "one"}),
                ToolCall("b", "tool_search", {"query": "two"}),
            ],
            "reads",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = AgentRuntime(
        Writer(), Adapter(turns), executor, system="solve", tools=[],
    )
    asyncio.run(runner.run_async("start"))
    assert executor.peak_concurrency == 1


def test_parallel_fan_out_is_capped():
    """Fan-out is provider-controlled, so it needs a ceiling.

    Each concurrent call takes a database connection and possibly a sandbox
    exec. A model emitting a dozen reads in one batch would exhaust the
    connection pool and stall the session rather than going faster.
    """
    executor = RecordingExecutor(delays={"memory_search": 0.03})
    reads = [ToolCall(f"r{i}", "memory_search", {"query": str(i)}) for i in range(9)]
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": call.id} for call in reads]}],
            reads,
            "many reads",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor, max_parallel_tools=3)
    asyncio.run(runner.run_async("start"))

    assert runner._tool_groups(reads) == [reads[0:3], reads[3:6], reads[6:9]]
    assert executor.peak_concurrency == 3
    assert len(executor.finished) == 9
    results = [
        item["payload"]["id"] for item in runner.writer.items
        if item["kind"] == "tool_result"
    ]
    assert results == [call.id for call in reads]


def test_one_failing_tool_does_not_discard_its_siblings_results():
    """A raise in a parallel group must not erase results that were known.

    The repair path turns a *missing* tool result into ``unknown`` on the next
    turn. So dropping a result the executor actually returned does not fail
    safe: it throws away real evidence and tells the model the call's outcome
    is unknown when it is not. The sequential path keeps every result up to
    the failure, and the parallel path has to match.
    """
    class Boom:
        def __init__(self):
            self.calls = []

        async def execute_async(self, name, arguments, *, idempotency_key):
            del idempotency_key
            self.calls.append(name)
            if name == "tool_search":
                raise RuntimeError("tool blew up")
            return {"ok": True, "tool": name}

    executor = Boom()
    turns = [
        Turn(
            [{"role": "assistant", "tool_calls": [{"id": "ok"}, {"id": "bad"}]}],
            [
                ToolCall("ok", "memory_search", {"query": "keep me"}),
                ToolCall("bad", "tool_search", {"query": "boom"}),
            ],
            "reads",
        ),
        Turn([{"role": "assistant", "content": "done"}], [], "done"),
    ]
    runner = runtime(turns, executor)
    with pytest.raises(RuntimeError, match="tool blew up"):
        asyncio.run(runner.run_async("start"))

    calls = [
        item["payload"]["id"] for item in runner.writer.items
        if item["kind"] == "tool_call"
    ]
    results = [
        item["payload"]["id"] for item in runner.writer.items
        if item["kind"] == "tool_result"
    ]
    assert calls == ["ok", "bad"]
    assert results == ["ok"]
