from __future__ import annotations

import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.diamond import Diamond
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.state import AppState


@pytest.fixture
def state(tmp_path, monkeypatch):
    import shutil

    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("config.yaml", "models.yaml", "limits.yaml"):
        shutil.copy(src / name, cfgdir / name)
    st = AppState(root=tmp_path, config_dir=cfgdir)
    # Force deterministic mock LLMs so tests never depend on real credentials
    # that may live in the shipped config.yaml.
    st.config.diamond.api_format = "mock"
    for m in st.config.members:
        m.api_format = "mock"
    st.config.runtime.sandbox_backend = "local"
    st.pool.backend = "local"
    st.network.backend = "local"
    return st


def _make_project(state, category="web"):
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, "Demo", "http://target", "capture the flag", category)
    return pid


def test_app_state_clean_start_wipes_runtime_state(tmp_path, monkeypatch):
    import shutil

    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("config.yaml", "models.yaml", "limits.yaml"):
        shutil.copy(src / name, cfgdir / name)

    data_dir = tmp_path / "data"
    st = AppState(root=tmp_path, config_dir=cfgdir)
    with st.db.connect() as conn:
        graph_store.create_project(conn, "Old", "origin", "goal", "web")
    (tmp_path / "logs" / "project_logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "project_logs" / "Old.json").write_text("old\n", encoding="utf-8")
    (tmp_path / "projects" / "proj_001" / "attachments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "proj_001" / "attachments" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "memory" / "export").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "export" / "memory.md").write_text("old", encoding="utf-8")
    (tmp_path / "wp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wp" / "Old.md").write_text("old", encoding="utf-8")

    monkeypatch.setenv("IPC_CLEAN_START", "1")
    cleaned = AppState(root=tmp_path, config_dir=cfgdir)

    with cleaned.db.connect() as conn:
        assert graph_store.project_summaries(conn) == []
        next_id = graph_store.create_project(conn, "New", "origin", "goal", "web")
    assert next_id == "proj_001"
    assert not (tmp_path / "logs" / "project_logs" / "Old.json").exists()
    assert not (tmp_path / "projects" / "proj_001" / "attachments" / "x.txt").exists()
    assert not (tmp_path / "memory" / "export" / "memory.md").exists()
    assert not (tmp_path / "wp" / "Old.md").exists()
    assert data_dir.exists()


def test_lifecycle_transitions(state):
    pid = _make_project(state)
    lc = Lifecycle(state.db)
    assert lc.status(pid) == "created"
    lc.transition(pid, "running")
    assert lc.status(pid) == "running"
    lc.transition(pid, "flag_found")
    with pytest.raises(LifecycleError):
        lc.transition(pid, "completed")  # must go through wp/memory writing


def test_diamond_initial_assignment(state):
    pid = _make_project(state)
    d = Diamond(state.db, state.config, state.logger)
    a = d.assign_initial(pid)
    assert a is not None
    assert a.member == "aventurine"
    assert a.is_initial is True
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert any(link.src == "ipc" and link.dst == "diamond" for link in detail.agent_links)
    assert any(link.src == "diamond" and link.dst == "aventurine" for link in detail.agent_links)
    assert "aventurine" in {ag.name for ag in detail.agents}


def test_diamond_reinforcements_on_high_difficulty(state):
    pid = _make_project(state)
    d = Diamond(state.db, state.config, state.logger)
    d.assign_initial(pid)  # aventurine active
    with state.db.connect() as conn:
        report = graph_store.create_report(
            conn, pid, "aventurine", "stuck on auth", "high", "origin",
            ["recon done"], ["try jwt forge", "try sqli", "try ssti"], ["web"],
        )
    assignments = d.decide_reinforcements(pid, report)
    # high difficulty -> up to 3, but capped by idle members (3 remaining)
    assert 1 <= len(assignments) <= 3
    names = {a.member for a in assignments}
    assert "aventurine" not in names  # initial member already active


def test_diamond_reinforcement_dedupes_duplicate_directions(state):
    pid = _make_project(state)
    d = Diamond(state.db, state.config, state.logger)
    d.assign_initial(pid)
    with state.db.connect() as conn:
        report = graph_store.create_report(
            conn, pid, "aventurine", "stuck on login", "high", "origin",
            ["recon done"], ["try sqli", "Try SQLi!!", "try sqli"], ["web"],
        )
    assignments = d.decide_reinforcements(pid, report)
    assert len(assignments) == 1
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    matching = [
        i for i in detail.intents
        if edge_store.normalize_intent_description(i.description) == "try sqli"
    ]
    assert len(matching) == 1


def test_diamond_no_reinforce_low_difficulty(state):
    pid = _make_project(state)
    d = Diamond(state.db, state.config, state.logger)
    d.assign_initial(pid)
    with state.db.connect() as conn:
        report = graph_store.create_report(
            conn, pid, "aventurine", "easy", "low", "origin", [], [], ["web"]
        )
    assert d.decide_reinforcements(pid, report) == []


def test_diamond_medium_difficulty_adds_one_helper(state):
    pid = _make_project(state)
    d = Diamond(state.db, state.config, state.logger)
    d.assign_initial(pid)
    with state.db.connect() as conn:
        report = graph_store.create_report(
            conn, pid, "aventurine", "branching web target", "medium", "origin",
            ["basic recon done"], ["check source leak", "check auth bypass"], ["web"],
        )
    assignments = d.decide_reinforcements(pid, report)
    assert len(assignments) == 1
    assert assignments[0].member != "aventurine"


def test_full_solve_pipeline_to_completed(state):
    """End-to-end: start -> initial solver -> WP -> memory -> IPC -> completed -> broadcast."""
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    orch = Orchestrator(state, max_workers=4)
    state.orchestrator = orch
    orch.start_project(pid)
    orch.wait(pid, timeout=20)
    # settle
    for _ in range(40):
        if Lifecycle(state.db).status(pid) == "completed":
            break
        time.sleep(0.1)
    status = Lifecycle(state.db).status(pid)
    assert status == "completed", f"status was {status}"
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, pid)
        assert row["flag"]
        assert row["wp_path"]
        broadcasts = graph_store.list_broadcasts(conn)
    assert any(b.project_id == pid for b in broadcasts)
    # WP file exists
    assert Path(row["wp_path"]).exists()
    assert Path(row["wp_path"]).name == "Demo.md"
    # memory was written (4 categories may not all fill, but at least exploit+knowledge)
    assert len(state.memory.all()) >= 1
    # completion graph links
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    kinds = {link.kind for link in detail.agent_links}
    assert "return" in kinds  # Diamond -> IPC
    orch.shutdown()


def test_reinforcement_pipeline_with_scripts(state):
    """A scripted member reports high difficulty -> Diamond adds members; initial script flags."""
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "crypto")
    # aventurine: report high difficulty then flag
    scripts = {
        "aventurine": [
            {"action": "report", "progress": "hard rsa", "difficulty": "high",
             "steps": ["got n,e,c"], "directions": ["lattice attack", "common modulus"], "knowledge": ["rsa"]},
            {"action": "flag", "flag": "flag{rsa_pwned}", "description": "broke rsa"},
        ],
    }
    orch = Orchestrator(state, max_workers=6, scripts=scripts)
    state.orchestrator = orch
    orch.start_project(pid)
    orch.wait(pid, timeout=20)
    for _ in range(40):
        if Lifecycle(state.db).status(pid) == "completed":
            break
        time.sleep(0.1)
    assert Lifecycle(state.db).status(pid) == "completed"
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    # reinforcements were assigned (more than just aventurine got an assign link)
    assigned = {link.dst for link in detail.agent_links if link.kind == "assign"}
    assert len(assigned) >= 2
    assert detail.project.flag == "flag{rsa_pwned}"
    orch.shutdown()


def test_orchestrator_dispatches_multiple_unclaimed_intents(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        old = edge_store.create_intent(conn, pid, ["origin"], "older branch", "diamond")
        newest = edge_store.create_intent(conn, pid, ["origin"], "newer branch", "diamond")

    orch = Orchestrator(state, max_workers=2)
    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))
        return True

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    assert launched == [
        (pid, "aventurine", newest.id, "web", False),
        (pid, "pearl", old.id, "web", False),
    ]
    assert old.id != newest.id
    orch.shutdown()


def test_orchestrator_dispatches_unclaimed_while_member_is_running(state):
    from backend.core.orchestrator import Orchestrator

    class RunningMember:
        def stop(self):
            pass

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        running = edge_store.create_intent(conn, pid, ["origin"], "running branch", "diamond", worker="aventurine")
        waiting = edge_store.create_intent(conn, pid, ["origin"], "parallel branch", "diamond")
        graph_store.add_agent(conn, pid, "aventurine", "member", state="active", start_fact_id="origin")

    orch = Orchestrator(state, max_workers=3)
    future = Future()
    with orch._lock:
        orch._members[pid] = {"aventurine": RunningMember()}
        orch._task_index[(pid, running.id)] = future
    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))
        return True

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    assert launched == [(pid, "pearl", waiting.id, "web", False)]
    with orch._lock:
        orch._members.pop(pid, None)
        orch._task_index.clear()
    orch.shutdown()


def test_orchestrator_prefers_creator_and_explore_member_for_new_intents(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.add_agent(conn, pid, "pearl", "member", state="idle", start_fact_id="origin")
        graph_store.add_agent(conn, pid, "jade", "member", state="idle", start_fact_id="origin")
        pearl_intent = edge_store.create_intent(conn, pid, ["origin"], "pearl follow-up", "pearl")
        jade_intent = edge_store.create_intent(conn, pid, ["origin"], "jade assigned branch", "diamond")
        graph_store.add_link(conn, pid, "jade", f"intent:{jade_intent.id}", "explore")

    orch = Orchestrator(state, max_workers=3)
    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))
        return True

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    by_intent = {intent_id: member_name for _, member_name, intent_id, _, _ in launched}
    assert by_intent[jade_intent.id] == "jade"
    assert by_intent[pearl_intent.id] == "pearl"
    orch.shutdown()


def test_member_assignment_records_graph_entry_fact(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        fact = node_store.create_fact(conn, pid, "confirmed leaked source code")
        intent = edge_store.create_intent(conn, pid, [fact.id], "exploit leaked source", "diamond")

    orch = Orchestrator(state, max_workers=2)
    orch._record_member_assignment(pid, "topaz", intent.id)

    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    topaz = next(agent for agent in detail.agents if agent.name == "topaz")
    assigned = next(i for i in detail.intents if i.id == intent.id)
    assert topaz.start_fact_id == fact.id
    assert topaz.state == "active"
    assert assigned.worker == "topaz"
    assert any(link.src == "diamond" and link.dst == "topaz" and link.kind == "assign" for link in detail.agent_links)
    assert any(link.src == "topaz" and link.dst == f"intent:{intent.id}" and link.kind == "explore" for link in detail.agent_links)
    orch.shutdown()


def test_reason_checkpoint_ignores_intent_created_by_reason(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    orch = Orchestrator(state, max_workers=2)
    with state.db.connect() as conn:
        before_reason = graph_store.project_detail(conn, pid)

    assert orch._reason_trigger(before_reason) == "initial"
    created = orch.diamond.plan_next_intent(pid, before_reason, "initial")
    assert created is not None
    orch._record_reason_checkpoint(pid, before_reason)

    with state.db.connect() as conn:
        after_reason = graph_store.project_detail(conn, pid)
    assert any(intent.id == created.id and intent.to is None for intent in after_reason.intents)
    assert orch._reason_trigger(after_reason) is None

    with state.db.connect() as conn:
        fact = node_store.create_fact(conn, pid, "confirmed new foothold")
        edge_store.conclude_intent(conn, pid, created.id, "aventurine", fact.id)
        changed = graph_store.project_detail(conn, pid)
    assert orch._reason_trigger(changed).startswith("facts:")
    orch.shutdown()
