from __future__ import annotations

import pytest

from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.db import Database
from backend.core.config import LLMConfig, MemberConfig
from backend.core.logging_util import IPCLogger
from backend.members.adapters import MemberAction, make_adapter
from backend.members.base_member import MemberDeps
from backend.members.factory import create_member
from backend.mcp.base import MCPRegistry
from backend.mcp.shared import build_browser_mcp
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.sandbox.sandbox import LocalSandbox
from backend.tools.tool_registry import ToolRegistry


@pytest.fixture
def deps(tmp_path):
    db = Database(tmp_path / "g.db").configure()
    mem = MemoryStore(tmp_path / "m.db").configure()
    reg = ToolRegistry(cache_db=tmp_path / "tc.db").load()
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


def _project(db):
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "T", "origin", "get flag", "web")
        intent = edge_store.create_intent(conn, pid, ["origin"], "explore web app", "diamond")
    return pid, intent.id


def test_adapter_factory_and_health():
    a = make_adapter(LLMConfig(api_format="mock"), name="aventurine")
    assert a.health()["ok"] is True


def test_member_action_parsing():
    act = MemberAction.from_obj({"action": "bash", "command": "ls", "thought": "list"})
    assert act.kind == "bash"
    assert act.args["command"] == "ls"
    with pytest.raises(ValueError):
        MemberAction.from_obj({"action": "nonsense"})


def test_initial_member_solves_to_flag(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    cfg = MemberConfig(name="aventurine", api_format="mock")
    member = create_member(cfg, d)
    result = member.solve(pid, iid, "web", is_initial=True)
    assert result.status == "flag"
    assert result.flag == "flag{test}"
    assert flags == [pid]
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert detail.project.status == "flag_found"
    assert detail.project.flag == "flag{test}"
    # a goal edge exists
    assert any(i.to == "goal" for i in detail.intents)


def test_followup_member_concludes(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    cfg = MemberConfig(name="pearl", api_format="mock")
    member = create_member(cfg, d)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "concluded"
    assert result.fact_id is not None
    with db.connect() as conn:
        row = edge_store.get_intent(conn, pid, iid)
    assert row["to_fact_id"] == result.fact_id


def test_member_reports_difficulty_on_eval_step(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    # script: bash 6 times, then nothing -> step 7 triggers evaluate_now -> mock reports
    script = [{"action": "bash", "command": f"echo {i}"} for i in range(6)]
    cfg = MemberConfig(name="aventurine", api_format="mock")
    # use default (non-script) behaviour but force eval interval small
    d.eval_interval = 3
    member = create_member(cfg, d)
    member.solve(pid, iid, "web", is_initial=False)
    # default arc concludes at step 3 which is also eval step -> report fires first
    assert len(reports) >= 1
    assert reports[0].difficulty in ("medium", "high")


def test_scripted_member_tool_and_done(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    script = [
        {"action": "tool", "server": "browser", "tool": "navigate", "args": {"url": "http://t"}},
        {"action": "memory", "query": "web ssti"},
        {"action": "done", "reason": "stop"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "done"
    assert any("mcp:browser.navigate" in o for o in member.observations)


def test_scripted_member_category_tools_mcp(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    script = [
        {"action": "tool", "server": "tools", "tool": "get_tool", "args": {"name": "sqlmap"}},
        {"action": "done", "reason": "checked tools"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)

    assert result.status == "done"
    assert any("mcp:tools.get_tool" in o and "sqlmap" in o for o in member.observations)
    context = member._build_context(pid, iid, "web", 1, False, False)
    assert "browser" in context["public_mcps"]
    assert "python" in context["available_languages"]


def test_member_report_defaults_to_low_when_unspecified(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    d.max_actions_per_task = 2
    script = [
        {"action": "report", "progress": "need a second look", "steps": ["recon"], "directions": ["check source"]},
        {"action": "done", "reason": "reported"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "done"
    assert len(reports) == 1
    assert reports[0].difficulty == "low"


def test_member_suppresses_same_difficulty_without_new_evidence(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    d.max_actions_per_task = 3
    script = [
        {"action": "report", "progress": "branching but stable", "difficulty": "medium",
         "steps": ["checked login"], "directions": ["continue auth"], "knowledge": ["web"]},
        {"action": "report", "progress": "still branching", "difficulty": "medium",
         "steps": ["checked login again"], "directions": ["continue auth"], "knowledge": ["web"]},
        {"action": "done", "reason": "reported"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    member.solve(pid, iid, "web", is_initial=False)
    assert len(reports) == 1
    assert reports[0].difficulty == "medium"


def test_member_reports_same_difficulty_when_evidence_accumulates(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    d.max_actions_per_task = 3
    script = [
        {"action": "report", "progress": "tried sqli", "difficulty": "medium",
         "steps": ["union select failed"], "directions": ["collect a second signal"], "knowledge": ["sqli"]},
        {"action": "report", "progress": "tried ssti too", "difficulty": "medium",
         "steps": ["jinja probe failed"], "directions": ["collect another signal"], "knowledge": ["ssti"]},
        {"action": "done", "reason": "reported"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    member.solve(pid, iid, "web", is_initial=False)
    assert len(reports) == 2
    assert reports[1].difficulty == "medium"
    assert "evidence:distinct_exploit_classes:2" in reports[1].knowledge


def test_no_new_fact_accumulates_for_same_intent_across_members(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    intent_tag = f"intent:{iid}"
    with db.connect() as conn:
        graph_store.create_report(
            conn, pid, "pearl", "no fact", "low", "origin",
            ["short task exhausted"], ["switch angle"], [intent_tag, "short_task_stall", "no_new_fact"],
        )
        graph_store.create_report(
            conn, pid, "topaz", "still no fact", "low", "origin",
            ["short task exhausted"], ["switch exploit class"], [intent_tag, "short_task_stall", "no_new_fact"],
        )
    d.max_actions_per_task = 2
    script = [
        {"action": "report", "progress": "third short task produced no new fact", "difficulty": "low",
         "steps": ["short task exhausted"], "directions": ["try a different attack surface"],
         "knowledge": ["short_task_stall", "no_new_fact"]},
        {"action": "done", "reason": "reported"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    member.solve(pid, iid, "web", is_initial=False)
    assert len(reports) == 1
    assert reports[0].difficulty == "high"
    assert "evidence:no_new_fact_short_tasks:3" in reports[0].knowledge


def test_member_duplicate_intent_does_not_count_as_progress(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    d.max_actions_per_task = 1
    script = [
        {"action": "intent", "from": ["origin"], "description": "Explore web app!!"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "stalled"
    assert len(reports) == 1
    assert reports[0].difficulty == "low"
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert [i.id for i in detail.intents] == [iid]


def test_member_creates_at_most_one_new_intent_per_task(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    d.max_actions_per_task = 3
    script = [
        {"action": "intent", "from": ["origin"], "description": "try admin path"},
        {"action": "intent", "from": ["origin"], "description": "try sql injection"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "done"
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    created = [i.description for i in detail.intents if i.creator == "jade"]
    assert created == ["try admin path"]


def test_member_defaults_new_intent_to_latest_fact_and_draws_link(deps):
    db, d, reports, flags = deps
    pid, iid = _project(db)
    with db.connect() as conn:
        fact = node_store.create_fact(conn, pid, "found debug endpoint")
    d.max_actions_per_task = 2
    script = [
        {"action": "intent", "description": "follow debug endpoint"},
        {"action": "done", "reason": "branched"},
    ]
    cfg = MemberConfig(name="jade", api_format="mock")
    member = create_member(cfg, d, script=script)
    result = member.solve(pid, iid, "web", is_initial=False)
    assert result.status == "done"
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    created = next(i for i in detail.intents if i.creator == "jade")
    assert created.from_ == [fact.id]
    assert any(l.src == "jade" and l.dst == f"intent:{created.id}" for l in detail.agent_links)
