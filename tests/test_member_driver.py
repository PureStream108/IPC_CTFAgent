"""Driver selection for the unified solving loop (P5).

Standalone projects and mock adapters now drive the provider-native streaming
runtime (``AgentRuntime``) instead of the legacy ``decide()`` loop. These tests
pin the three contracts:

* a standalone scripted member runs the streaming driver and lands a durable
  ``project`` agent session with its tool events;
* the same script driven by both drivers leaves identical graph writes;
* the legacy loop survives as an explicit, observable fallback.
"""
from __future__ import annotations

import pytest

from backend.agent.session import AgentSessionStore
from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.db import Database
from backend.core.config import MemberConfig
from backend.core.logging_util import IPCLogger
from backend.members import base_member
from backend.members.adapters import MemberAction
from backend.members.base_member import MemberDeps
from backend.members.factory import create_member
from backend.mcp.mcp_client import MCPRegistry
from backend.mcp.shared import build_browser_mcp
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.sandbox.sandbox import LocalSandbox
from backend.tools.tool_registry import ToolRegistry


@pytest.fixture
def deps(tmp_path):
    db = Database().configure()
    mem = MemoryStore(db).configure()
    reg = ToolRegistry().load()
    mcps = MCPRegistry()
    mcps.register(build_memory_mcp(mem))
    mcps.register(build_browser_mcp())
    sb = LocalSandbox("test", tmp_path / "ws")
    sb.start()
    reports = []
    flags = []
    d = MemberDeps(
        db=db, logger=IPCLogger(tmp_path / "logs", enabled=True), sandbox=sb,
        mcps=mcps, registry=reg, memory=mem, eval_interval=7, max_steps=20,
        on_report=lambda pid, r: reports.append(r),
        on_flag=lambda pid: flags.append(pid),
        expected_flag="flag{test}",
    )
    return db, d, reports, flags


def _project(db, title="T"):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, title, "origin", "get flag", "web")
        intent = edge_store.create_intent(conn, pid, ["origin"], "explore web app", "diamond")
    return pid, intent.id


EQUIVALENCE_SCRIPT = [
    {"action": "bash", "command": "echo recon"},
    {"action": "intent", "from": ["origin"], "description": "probe the login form"},
    {"action": "report", "progress": "found a login form", "difficulty": "medium",
     "steps": ["recon"], "directions": ["try sqli"], "knowledge": ["web"]},
    {"action": "conclude", "description": "login form confirmed present"},
]


def _graph_snapshot(db, pid):
    with db.connect() as conn:
        facts = {fact.id: fact.description for fact in node_store.list_facts(conn, pid)}
        intents = [
            (
                intent.description,
                tuple(facts.get(fid, fid) for fid in (intent.from_ or ())),
                facts.get(intent.to, intent.to),
            )
            for intent in edge_store.list_intents(conn, pid)
        ]
        reports = [
            (r.member, r.progress, r.difficulty, tuple(r.steps), tuple(r.directions),
             tuple(r.knowledge))
            for r in graph_store.list_reports(conn, pid)
        ]
    return sorted(facts.values()), sorted(intents), sorted(reports)


def test_standalone_member_runs_the_streaming_driver(deps):
    db, d, _reports, _flags = deps
    pid, iid = _project(db)
    member = create_member(
        MemberConfig(name="jade", api_format="mock"),
        d,
        script=[{"action": "done", "reason": "streamed"}],
    )

    result = member.solve(pid, iid, "web", is_initial=False)

    assert result.status == "done"
    store = AgentSessionStore(db)
    session = store.active_session_for_task(f"member:{pid}:jade:{iid}")
    assert session is not None
    assert session["kind"] == "project"
    events = store.events(session["id"], after=0, limit=500)
    assert any(event["kind"] == "tool_call" for event in events)
    assert any(event["kind"] == "tool_result" for event in events)
    entries = d.logger.read_log("project", pid, None)
    assert not any(entry["event"] == "member_driver" for entry in entries)
    assert any(entry["event"] == "member_done" for entry in entries)


def test_streaming_and_legacy_drivers_leave_identical_graph_writes(deps, monkeypatch):
    db, d, _reports, _flags = deps
    pid_stream, iid_stream = _project(db, "stream")
    member = create_member(
        MemberConfig(name="jade", api_format="mock"), d, script=EQUIVALENCE_SCRIPT
    )
    streamed = member.solve(pid_stream, iid_stream, "web", is_initial=False)
    assert streamed.status == "concluded"

    monkeypatch.setattr(base_member, "MEMBER_LEGACY_DRIVER", True)
    pid_legacy, iid_legacy = _project(db, "legacy")
    legacy_member = create_member(
        MemberConfig(name="jade", api_format="mock"), d, script=EQUIVALENCE_SCRIPT
    )
    legacied = legacy_member.solve(pid_legacy, iid_legacy, "web", is_initial=False)
    assert legacied.status == "concluded"

    assert legacied.steps == streamed.steps
    assert _graph_snapshot(db, pid_legacy) == _graph_snapshot(db, pid_stream)


def test_legacy_fallback_is_observable(deps, monkeypatch):
    db, d, _reports, _flags = deps
    monkeypatch.setattr(base_member, "MEMBER_LEGACY_DRIVER", True)
    pid, iid = _project(db)
    member = create_member(
        MemberConfig(name="jade", api_format="mock"),
        d,
        script=[{"action": "done", "reason": "legacy path"}],
    )

    result = member.solve(pid, iid, "web", is_initial=False)

    assert result.status == "done"
    entries = d.logger.read_log("project", pid, None)
    driver = [entry for entry in entries if entry["event"] == "member_driver"]
    assert len(driver) == 1
    assert driver[0]["driver"] == "legacy_fallback"
    assert driver[0]["reason"] == "feature_flag"
    store = AgentSessionStore(db)
    assert store.active_session_for_task(f"member:{pid}:jade:{iid}") is None


def test_adapter_without_turn_surface_falls_back_to_legacy(deps):
    db, d, _reports, _flags = deps
    pid, iid = _project(db)
    member = create_member(MemberConfig(name="jade", api_format="mock"), d)

    class DecideOnly:
        def decide(self, context):
            return MemberAction.from_obj({"action": "done", "reason": "no turn surface"})

    member.adapter = DecideOnly()
    result = member.solve(pid, iid, "web", is_initial=False)

    assert result.status == "done"
    entries = d.logger.read_log("project", pid, None)
    driver = [entry for entry in entries if entry["event"] == "member_driver"]
    assert len(driver) == 1
    assert driver[0]["driver"] == "legacy_fallback"
    assert driver[0]["reason"] == "adapter_without_turn_surface"
