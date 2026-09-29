from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.mcp.mcp_client import MCPClient
from backend.ops.ipc_mcp import _session_id_from_context, build_ipc_mcp


def call_tool(server, name: str, **arguments):
    async def run():
        async with MCPClient.in_process(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


class _Competition:
    def __init__(self):
        self.calls: list[tuple] = []
        self.store = self

    def preflight(self, workflow_id):
        self.calls.append(("preflight", workflow_id))
        return {
            "workflow_id": workflow_id,
            "identity_key": "fixture:team",
            "challenge_count": 10,
            "capabilities": {"submit": True, "instances": False},
        }

    def start(self, workflow_id, idempotency_key, select=None):
        self.calls.append(("start", workflow_id, idempotency_key, select))
        return {
            "run": {"id": "run-fixture", "status": "running"},
            "capacity": {"total": 10, "solving": 0, "idle": 10},
        }

    def run_snapshot(self, run_id):
        self.calls.append(("snapshot", run_id))
        return {"run": {"id": run_id, "status": "running"}, "capacity": {"total": 10}}

    def member_snapshot(self, run_id):
        self.calls.append(("members", run_id))
        return [{"member": "amber", "state": "idle", "assignment": None}]


def test_ipc_platform_allocation_tools_delegate_to_competition_service():
    competition = _Competition()
    state = SimpleNamespace(competition=competition)
    server = build_ipc_mcp(lambda: state)

    preflight = call_tool(server, "ipc_platform_preflight", workflow_id="wf_fixture")
    assert preflight["ok"] is True
    assert preflight["allocator"] == "ipc"
    assert preflight["challenge_count"] == 10

    started = call_tool(
        server,
        "ipc_start_platform",
        workflow_id="wf_fixture",
        idempotency_key="fixture-start-1",
    )
    assert started["ok"] is True
    assert started["run"]["id"] == "run-fixture"
    assert started["capacity"]["total"] == 10

    status = call_tool(server, "ipc_platform_status", run_id="run-fixture")
    assert status["ok"] is True
    assert status["members"][0]["member"] == "amber"
    assert competition.calls == [
        ("preflight", "wf_fixture"),
        ("start", "wf_fixture", "fixture-start-1", None),
        ("snapshot", "run-fixture"),
        ("members", "run-fixture"),
    ]


def test_ipc_start_platform_passes_challenge_selection():
    competition = _Competition()
    server = build_ipc_mcp(lambda: SimpleNamespace(competition=competition))

    started = call_tool(
        server,
        "ipc_start_platform",
        workflow_id="wf_fixture",
        idempotency_key="fixture-start-select",
        select=["1129", "1208"],
    )

    assert started["ok"] is True
    assert competition.calls == [
        ("start", "wf_fixture", "fixture-start-select", ["1129", "1208"])
    ]


def test_ipc_platform_start_rejects_short_idempotency_key_before_dispatch():
    competition = _Competition()
    server = build_ipc_mcp(lambda: SimpleNamespace(competition=competition))

    result = call_tool(
        server,
        "ipc_start_platform",
        workflow_id="wf_fixture",
        idempotency_key="short",
    )

    assert result == {
        "ok": False,
        "workflow_id": "wf_fixture",
        "error": "idempotency_key must contain at least 8 characters",
    }
    assert competition.calls == []


def test_ipc_mcp_exposes_question_probe_and_requires_valid_session_header():
    server = build_ipc_mcp(lambda: SimpleNamespace())
    names = {
        tool.name
        for tool in asyncio.run(_list_tools(server))
    }
    assert {"ipc_question", "ipc_question_result"} <= names

    valid = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-ipc-ops-session": "ops_0123456789abcdef"})
        )
    )
    invalid = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-ipc-ops-session": "not-a-session"})
        )
    )
    assert _session_id_from_context(valid) == "ops_0123456789abcdef"
    assert _session_id_from_context(invalid) is None


async def _list_tools(server):
    async with MCPClient.in_process(server) as client:
        return await client.list_tools()
