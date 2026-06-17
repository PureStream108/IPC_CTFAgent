from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.blackboard import edge_store, graph_store
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
    assert any(l.src == "ipc" and l.dst == "diamond" for l in detail.agent_links)
    assert any(l.src == "diamond" and l.dst == "aventurine" for l in detail.agent_links)
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
    kinds = {l.kind for l in detail.agent_links}
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
    assigned = {l.dst for l in detail.agent_links if l.kind == "assign"}
    assert len(assigned) >= 2
    assert detail.project.flag == "flag{rsa_pwned}"
    orch.shutdown()


def test_orchestrator_prefers_newest_unclaimed_intent(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        old = edge_store.create_intent(conn, pid, ["origin"], "older branch", "diamond")
        newest = edge_store.create_intent(conn, pid, ["origin"], "newer branch", "diamond")

    orch = Orchestrator(state, max_workers=2)
    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    assert launched == [(pid, "aventurine", newest.id, "web", False)]
    assert old.id != newest.id
    orch.shutdown()
