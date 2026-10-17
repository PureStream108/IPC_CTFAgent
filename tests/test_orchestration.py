from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.diamond import Diamond
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.postprocess_store import (
    claim_next_job,
    complete_existing_job,
    complete_job,
    enqueue_postprocess,
    fail_job,
    recover_expired_jobs,
)
from backend.core.state import AppState
from tests.helpers import write_mock_config


@pytest.fixture
def state(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")
    st = AppState(root=tmp_path, config_dir=cfgdir)
    st.pool.backend = "local"
    st.network.backend = "local"
    try:
        yield st
    finally:
        if st.orchestrator is not None:
            st.orchestrator.shutdown()
        st.close()


def _make_project(state, category="web"):
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, "Demo", "http://target", "capture the flag", category)
    return pid


@pytest.mark.parametrize("other_project", [False, True])
def test_postprocess_claim_skips_a_project_locked_by_completion(state, other_project):
    project_id = _make_project(state)
    other_id = _make_project(state) if other_project else None
    with state.db.connect() as connection:
        enqueue_postprocess(connection, project_id)
        if other_id:
            enqueue_postprocess(connection, other_id)

    def claim():
        with state.db.connect() as connection:
            return claim_next_job(connection, "concurrent-worker")

    with ThreadPoolExecutor(max_workers=1) as executor, state.db.connect() as completion:
        completion.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
        claimed = executor.submit(claim).result(timeout=5)
        assert (claimed.project_id if claimed else None) == other_id
        completion.execute("SET LOCAL lock_timeout='2s'")
        enqueue_postprocess(completion, project_id)
    with state.db.connect() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM postprocess_jobs WHERE project_id=%s", (project_id,),
        ).fetchone()["n"] == 3


def test_expired_postprocess_recovery_skips_an_active_completion_transaction(state):
    project_id = _make_project(state)
    with state.db.connect() as connection:
        enqueue_postprocess(connection, project_id)
        stale = claim_next_job(connection, "stale-worker")
        connection.execute(
            "UPDATE postprocess_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (stale.id,),
        )

    def recover():
        with state.db.connect() as connection:
            return recover_expired_jobs(connection)

    with ThreadPoolExecutor(max_workers=1) as executor, state.db.connect() as completion:
        completion.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
        assert executor.submit(recover).result(timeout=5) == 0
        completion.execute("SET LOCAL lock_timeout='2s'")
        enqueue_postprocess(completion, project_id)
    with state.db.connect() as connection:
        assert recover_expired_jobs(connection) == 1
        assert claim_next_job(connection, "replacement-worker").id == stale.id


def test_real_orchestrator_starts_local_and_remote_tasks_with_independent_capacity(state):
    import hashlib
    import json

    from psycopg.types.json import Jsonb

    from backend.competition.fixture import FixturePlatform
    from backend.competition.service import CompetitionService
    from backend.core.orchestrator import Orchestrator
    from backend.platform.mapping import PlatformChallenge
    from tests.test_competition_service_fixture import _FixtureOps, _spec

    state.config.limits.max_concurrent_tasks = 1
    state.limiter.update_limits(max_concurrent_tasks=1)
    orch = Orchestrator(state, scripts={name: [
        {"action": "bash", "command": 'python -c "import time; time.sleep(2)"'},
        {"action": "done", "reason": "capacity fixture complete"},
    ] for name in (m.name for m in state.config.available_members())})
    state.orchestrator = orch
    spec = _spec()
    digest = hashlib.sha256(json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    with state.db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows (id,source,name,spec_json,spec_digest,status,confirmed_digest,created_at,updated_at)
               VALUES ('capacity-workflow','fixture',%s,%s,%s,'confirmed',%s,now(),now())""",
            (spec.name, Jsonb(spec.model_dump(mode="json")), digest, digest),
        )
    workflow = {"id": "capacity-workflow", "status": "confirmed", "spec_digest": digest,
                "confirmed_digest": digest, "spec": spec}
    platform = FixturePlatform([
        PlatformChallenge(external_id="local", title="Local", category="misc", description="capacity"),
        PlatformChallenge(external_id="remote", title="Remote", category="web", description="capacity", remote=True),
    ], remote_limit=1)
    service = CompetitionService(state, platform_factory=lambda *_args: platform,
                                 ops_service=_FixtureOps(state.db, workflow))
    state.competition = service
    try:
        run_id = service.start(workflow["id"], "capacity-start")["run"]["id"]
        service.tick()
        projects = {row["project_id"] for row in service.store.challenges(run_id)}
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if projects.issubset(set(orch.active_member_owners().values())):
                break
            time.sleep(0.02)
        assert projects.issubset(set(orch.active_member_owners().values()))
        assert projects.issubset(set(state.limiter.active_tasks()))
        until = time.monotonic() + 5
        remote = next(row for row in service.store.challenges(run_id) if row["external_id"] == "remote")
        seat = next(row for row in service.store.assignments(run_id, active_only=True) if row["challenge_id"] == remote["id"])
        while time.monotonic() < until:
            events = service.store.events(seat["session_id"], limit=500)
            tools = [row for row in events if row["kind"] == "tool_call"]
            if tools:
                break
            time.sleep(0.02)
        target = next(row for row in events if row["event_key"].startswith("recon:") and "target_instance" in str(row["payload"]))
        assert tools and target["sequence"] < tools[0]["sequence"]
        run = service.store.get_run(run_id)
        service.control(run_id, "stop", run["revision"])
        assert not projects.intersection(state.limiter.active_tasks())
    finally:
        service.shutdown()


@pytest.mark.parametrize("mode", ["recovered", "capacity", "sandbox_failure"])
def test_running_project_recovery_preserves_state_and_releases_failed_capacity(state, monkeypatch, mode):
    from backend.core.orchestrator import Orchestrator
    from backend.sandbox.errors import SandboxStartupError

    pid = _make_project(state)
    with state.db.connect() as connection:
        graph_store.set_status(connection, pid, "running")
    orch = Orchestrator(state, max_workers=1)
    state.orchestrator = orch
    dispatched = []
    monkeypatch.setattr(orch, "_dispatch_project", dispatched.append)
    if mode == "capacity":
        monkeypatch.setattr(orch.resources, "acquire_task", lambda _pid: False)
    elif mode == "sandbox_failure":
        def fail_preflight(_pid):
            raise SandboxStartupError("daemon unavailable", operation="recovery preflight")
        monkeypatch.setattr(orch.resources, "preflight_project", fail_preflight)

    orch._startup_project_ids.add(pid)
    orch._recover_running_project(pid)

    assert not orch.startup_in_progress(pid)
    status = orch.runtime_status(pid)
    if mode == "recovered":
        assert dispatched == [pid]
        assert status["status"] == "running" and status["phase"] == "ready"
    elif mode == "capacity":
        assert dispatched == []
        assert status["phase"] == "queued_capacity"
        assert pid in orch._pending_project_ids
        assert pid not in orch._project_leases
    else:
        assert dispatched == []
        assert status["status"] == "infra_error"
        assert pid not in state.limiter.active_tasks()
        assert pid not in orch._project_leases


@pytest.mark.parametrize("status", ["running", "flag_found", "solved"])
def test_startup_failure_fences_workers_but_preserves_flag_state(state, status):
    from backend.core.orchestrator import Orchestrator
    from backend.sandbox.errors import SandboxStartupError

    pid = _make_project(state)
    with state.db.connect() as connection:
        graph_store.set_status(connection, pid, status)
    orch = Orchestrator(state, max_workers=1)
    state.orchestrator = orch
    assert orch._ensure_project_lease(pid)
    orch.resources.acquire_task(pid)
    orch._mark_project_startup_failure(
        pid, SandboxStartupError("cannot start", operation="preflight"), event="startup.failed",
    )
    assert orch.runtime_status(pid)["status"] == ("infra_error" if status == "running" else status)
    assert pid not in state.limiter.active_tasks()
    assert pid not in orch._project_leases


@pytest.mark.parametrize("description,expected", [
    ("Found flag{followup} in the response", 1),
    ("Found flag{first} and ctf{second}", 0),
    ("No candidate in this observation", 0),
])
def test_concluded_flag_evidence_requires_one_idempotent_submission_followup(state, description, expected):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state)
    orch = Orchestrator(state, max_workers=1)
    state.orchestrator = orch
    with state.db.connect() as connection:
        fact = node_store.reserve_fact(connection, pid, description)
        node_store.insert_fact(connection, pid, fact.id, fact.description)
    orch._flag_followup_from_fact(pid, fact.id)
    orch._flag_followup_from_fact(pid, fact.id)
    with state.db.connect() as connection:
        intents = edge_store.list_intents(connection, pid)
        submissions = connection.execute("SELECT 1 FROM flag_submissions WHERE project_id=%s", (pid,)).fetchall()
    followups = [intent for intent in intents if "submit_flag tool" in intent.description]
    assert len(followups) == expected
    assert submissions == []


def test_app_state_clean_start_wipes_files_but_preserves_postgres(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")

    st = AppState(root=tmp_path, config_dir=cfgdir)
    with st.db.connect() as conn:
        graph_store.create_project(conn, "Old", "origin", "goal", "web")
    (st.logs_dir / "project_logs").mkdir(parents=True, exist_ok=True)
    (st.logs_dir / "project_logs" / "Old.json").write_text("old\n", encoding="utf-8")
    (st.projects_dir / "proj_001" / "attachments").mkdir(parents=True, exist_ok=True)
    (st.projects_dir / "proj_001" / "attachments" / "x.txt").write_text("x", encoding="utf-8")
    (st.export_dir / "memory" / "memory.md").parent.mkdir(parents=True, exist_ok=True)
    (st.export_dir / "memory" / "memory.md").write_text("old", encoding="utf-8")
    (st.wp_dir / "Old.md").write_text("old", encoding="utf-8")
    st.close()

    monkeypatch.setenv("IPC_CLEAN_START", "1")
    cleaned = AppState(root=tmp_path, config_dir=cfgdir)
    try:
        with cleaned.db.connect() as conn:
            summaries = graph_store.project_summaries(conn)
            assert [project.title for project in summaries] == ["Old"]
            next_id = graph_store.create_project(conn, "New", "origin", "goal", "web")
        assert next_id == "proj_002"
        assert not (st.logs_dir / "project_logs" / "Old.json").exists()
        assert not (st.projects_dir / "proj_001" / "attachments" / "x.txt").exists()
        assert (st.export_dir / "memory" / "memory.md").exists()
        assert not (st.wp_dir / "Old.md").exists()
    finally:
        cleaned.close()


def test_lifecycle_transitions(state):
    pid = _make_project(state)
    lc = Lifecycle(state.db)
    assert lc.status(pid) == "created"
    lc.transition(pid, "running")
    assert lc.status(pid) == "running"
    lc.transition(pid, "flag_found")
    with pytest.raises(LifecycleError):
        lc.transition(pid, "completed")


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
    d.assign_initial(pid)
    with state.db.connect() as conn:
        report = graph_store.create_report(
            conn, pid, "aventurine", "stuck on auth", "high", "origin",
            ["recon done"], ["try jwt forge", "try sqli", "try ssti"], ["web"],
        )
    assignments = d.decide_reinforcements(pid, report)
    assert 1 <= len(assignments) <= 3
    names = {a.member for a in assignments}
    assert "aventurine" not in names


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


def test_full_solve_pipeline_to_solved(state):
    """End-to-end: start -> solver -> verified Flag -> asynchronous postprocess."""
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    scripts = {
        "aventurine": [
            {"action": "bash", "command": "echo recon on web challenge"},
            {"action": "memory", "query": "web capture the flag"},
            {"action": "flag", "flag": "flag{mock_solved}",
             "description": "Captured flag via the scripted mock arc."},
        ],
    }
    orch = Orchestrator(state, max_workers=4, scripts=scripts)
    state.orchestrator = orch
    orch.start_project(pid)
    orch.wait(pid, timeout=20)
    for _ in range(40):
        if Lifecycle(state.db).status(pid) == "solved":
            break
        time.sleep(0.1)
    status = Lifecycle(state.db).status(pid)
    assert status == "solved", f"status was {status}"
    deadline = time.monotonic() + 10
    row = None
    broadcasts = []
    while time.monotonic() < deadline:
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, pid)
            broadcasts = graph_store.list_broadcasts(conn)
        if row is not None and row["postprocess_status"] == "completed":
            break
        time.sleep(0.05)
    assert row is not None
    assert row["flag"]
    assert row["wp_path"]
    assert row["postprocess_status"] == "completed"
    assert any(b.project_id == pid for b in broadcasts)
    assert Path(row["wp_path"]).exists()
    assert Path(row["wp_path"]).name == "Demo.md"
    wp_content = Path(row["wp_path"]).read_text(encoding="utf-8")
    assert "```python" in wp_content
    assert row["flag"] in wp_content
    assert "The CTF challenge is complete" not in wp_content
    assert (state.wp_export_dir / "Demo.md").is_file()
    for folder in state.logger.KINDS.values():
        assert (state.log_export_dir / folder / "Demo.log").is_file()
    assert len(state.memory.all()) >= 1
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    kinds = {link.kind for link in detail.agent_links}
    assert "return" in kinds
    orch.shutdown()
    state.orchestrator = None


def test_postprocess_worker_fencing_rejects_stale_completion(state):
    """A reclaimed postprocess job cannot be completed by its old worker."""

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "solved")
        enqueue_postprocess(conn, pid)
        stale = claim_next_job(conn, "worker-old", lease_seconds=120)
        assert stale is not None
        conn.execute(
            "UPDATE postprocess_jobs SET lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = %s",
            (stale.id,),
        )
        assert complete_job(conn, stale) is False
        assert complete_existing_job(
            conn, pid, stale.kind, lease_token=stale.lease_token
        ) is False
        assert fail_job(conn, stale, "late worker failure") is False
        expired = conn.execute(
            "SELECT status, locked_by, lease_token FROM postprocess_jobs WHERE id = %s",
            (stale.id,),
        ).fetchone()
        assert expired["status"] == "running"
        assert expired["locked_by"] == "worker-old"
        assert expired["lease_token"] == stale.lease_token

        assert recover_expired_jobs(conn) == 1
        replacement = claim_next_job(conn, "worker-new", lease_seconds=120)
        assert replacement is not None
        assert replacement.id == stale.id
        assert replacement.lease_token != stale.lease_token

        assert complete_job(conn, stale) is False
        row = conn.execute(
            "SELECT status, locked_by, lease_token FROM postprocess_jobs WHERE id = %s",
            (stale.id,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["locked_by"] == "worker-new"
        assert row["lease_token"] == replacement.lease_token

        assert complete_job(conn, replacement) is True
        completed = conn.execute(
            "SELECT status, locked_by, lease_token, lease_expires_at "
            "FROM postprocess_jobs WHERE id = %s",
            (stale.id,),
        ).fetchone()
    assert completed == {
        "status": "completed",
        "locked_by": None,
        "lease_token": None,
        "lease_expires_at": None,
    }


def test_reinforcement_pipeline_with_scripts(state):
    """A scripted member reports high difficulty -> Diamond adds members; initial script flags."""
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "crypto")
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
        if Lifecycle(state.db).status(pid) == "solved":
            break
        time.sleep(0.1)
    assert Lifecycle(state.db).status(pid) == "solved"
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
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


def test_orchestrator_adds_members_without_per_member_admission(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        intent = edge_store.create_intent(conn, pid, ["origin"], "launch jade", "diamond")

    state.config.runtime.sandbox_backend = "local"
    state.config.runtime.max_member_actions_per_task = 1
    state.limiter.max_concurrent_tasks = 1
    assert state.limiter.acquire(pid) is True
    state.pool.get(pid, "jade")

    orch = Orchestrator(
        state,
        max_workers=1,
        scripts={"jade": [{"action": "done", "reason": "reuse ok"}]},
    )
    launched = orch._launch_member(pid, "jade", intent.id, "web", False)
    deadline = time.time() + 5
    while time.time() < deadline:
        with orch._lock:
            futures = list(orch._futures.get(pid, []))
        if futures and all(future.done() for future in futures):
            break
        time.sleep(0.05)

    assert launched is True
    entries = state.logger.read_log("project", pid, None)
    assert not any(entry["event"] == "member_admission_denied" for entry in entries)
    assert any(entry["event"] == "member_done" and entry["member"] == "jade" for entry in entries)
    orch.shutdown()


def test_start_project_async_redispatches_owned_running_project(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        edge_store.create_intent(conn, pid, ["origin"], "keep working", "diamond")
        graph_store.set_status(conn, pid, "running")

    state.config.runtime.sandbox_backend = "local"
    state.limiter.max_concurrent_tasks = 1
    assert state.limiter.acquire(pid) is True
    state.pool.get(pid, "jade")

    orch = Orchestrator(
        state,
        max_workers=1,
        scripts={
            name: [{"action": "done", "reason": "redispatch ok"}]
            for name in (
                "amber", "agate", "topaz", "sugilite", "aventurine",
                "pearl", "sapphire", "jade", "obsidian", "opal",
            )
        },
    )
    assert orch._ensure_project_lease(pid) is True

    orch.start_project_async(pid)
    launched = False
    deadline = time.time() + 10
    while time.time() < deadline:
        with orch._lock:
            futures = list(orch._futures.get(pid, []))
        if futures:
            launched = True
            if all(future.done() for future in futures):
                break
        time.sleep(0.05)

    assert launched
    entries = state.logger.read_log("project", pid, None)
    assert any(entry["event"] == "member_done" for entry in entries)
    orch.shutdown()


def test_dispatch_project_launches_intent_created_by_reason(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")

    state.config.runtime.sandbox_backend = "local"
    state.limiter.max_concurrent_tasks = 1
    assert state.limiter.acquire(pid) is True
    state.pool.get(pid, "jade")

    orch = Orchestrator(
        state,
        max_workers=1,
        scripts={
            name: [{"action": "done", "reason": "reason dispatch ok"}]
            for name in (
                "amber", "agate", "topaz", "sugilite", "aventurine",
                "pearl", "sapphire", "jade", "obsidian", "opal",
            )
        },
    )
    assert orch._ensure_project_lease(pid) is True

    orch._dispatch_project(pid)
    launched = False
    deadline = time.time() + 10
    while time.time() < deadline:
        with orch._lock:
            futures = list(orch._futures.get(pid, []))
        if futures:
            launched = True
            if all(future.done() for future in futures):
                break
        time.sleep(0.05)

    assert launched
    entries = state.logger.read_log("project", pid, None)
    assert any(entry["event"] == "diamond_reason_planned" for entry in entries)
    assert any(entry["event"] == "member_done" for entry in entries)
    orch.shutdown()


def test_task_slot_queue_starts_next_project_after_release(state):
    from backend.core.orchestrator import Orchestrator

    project_ids = [_make_project(state, "web") for _ in range(3)]
    state.limiter.max_concurrent_tasks = 2
    orch = Orchestrator(state, max_workers=1)
    orch._launch_member = lambda *args, **kwargs: True

    for project_id in project_ids:
        orch.start_project(project_id)

    assert Lifecycle(state.db).status(project_ids[0]) == "running"
    assert Lifecycle(state.db).status(project_ids[1]) == "running"
    assert Lifecycle(state.db).status(project_ids[2]) == "created"
    assert list(orch._pending_projects) == [project_ids[2]]

    with state.db.connect() as conn:
        graph_store.set_status(conn, project_ids[0], "stopped")
    orch.stop_project(project_ids[0])
    orch._tick()

    deadline = time.time() + 2
    while Lifecycle(state.db).status(project_ids[2]) != "running" and time.time() < deadline:
        time.sleep(0.01)
    assert Lifecycle(state.db).status(project_ids[2]) == "running"
    assert state.limiter.active_tasks() == sorted(project_ids[1:])
    assert list(orch._pending_projects) == []
    orch.shutdown()


def test_stop_project_leaves_durable_stopped_status(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state)
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
    orch = Orchestrator(state, max_workers=1)
    orch.stop_project(pid)

    assert Lifecycle(state.db).status(pid) == "stopped"
    orch._tick()
    assert Lifecycle(state.db).status(pid) == "stopped"
    orch.shutdown()


def test_stop_project_preserves_solved_status(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state)
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
        graph_store.set_status(conn, pid, "flag_found")
        graph_store.set_status(conn, pid, "solved")
    orch = Orchestrator(state, max_workers=1)
    orch.stop_project(pid)

    assert Lifecycle(state.db).status(pid) == "solved"
    orch.shutdown()


def test_orchestrator_builds_task_container_mcp_targets(state):
    from backend.core.orchestrator import Orchestrator

    state.pool.backend = "docker"
    orch = Orchestrator(state, max_workers=1)
    targets = orch._container_mcps("proj_001", "aventurine")

    assert set(targets) == {"browser", "reverse"}
    reverse_args = targets["reverse"].target.args
    assert reverse_args == [
        "exec", "-i", "-w", "/workspace/shared", "ipc-task-proj_001",
        "python3", "-m", "backend.mcp.mcp_server", "reverse",
    ]
    assert targets["reverse"].read_timeout == 600
    browser_args = targets["browser"].target.args
    assert "IPC_BROWSER_PROJECT_ID=proj_001" in browser_args
    assert "IPC_BROWSER_MEMBER=aventurine" in browser_args
    assert "IPC_BROWSER_WORKDIR=/workspace/shared" in browser_args
    assert "IPC_BROWSER_SHARED_DIR=/workspace/shared" in browser_args
    assert "IPC_BROWSER_ARTIFACT_ROOT=/workspace/shared/browser-artifacts" in browser_args
    assert "IPC_BROWSER_EVENT_LIMIT=200" in browser_args
    assert "IPC_BROWSER_RESPONSE_PREVIEW_BYTES=4096" in browser_args
    assert "IPC_BROWSER_ALLOWED_ORIGINS=[]" in browser_args
    assert browser_args[-7:] == [
        "-w", "/workspace/shared", "ipc-task-proj_001",
        "python3", "-m", "backend.mcp.mcp_server", "browser",
    ]

    assert set(targets) == {"browser", "reverse"}
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
        token = edge_store.claim_intent(conn, pid, created.id, "aventurine")
        assert token
        assert edge_store.conclude_intent(
            conn,
            pid,
            created.id,
            "aventurine",
            fact.id,
            lease_owner="aventurine",
            lease_token=token,
        )
        changed = graph_store.project_detail(conn, pid)
    assert orch._reason_trigger(changed).startswith("facts:")
    orch.shutdown()


def test_reason_checkpoint_stable_state_does_not_log_skipped_noise(state):
    from backend.core.orchestrator import Orchestrator, ReasonCheckpoint

    class RunningMember:
        def stop(self):
            pass

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
        intent = edge_store.create_intent(
            conn,
            pid,
            ["origin"],
            "active branch",
            "diamond",
            worker="aventurine",
        )
        graph_store.add_agent(conn, pid, "aventurine", "member", state="active", start_fact_id="origin")
        detail = graph_store.project_detail(conn, pid)

    orch = Orchestrator(state, max_workers=3)
    orch._reason_checkpoints[pid] = ReasonCheckpoint(
        fact_count=len(detail.facts),
        hint_count=len(detail.hints),
        open_intent_count=1,
    )
    with orch._lock:
        orch._members[pid] = {"aventurine": RunningMember()}

    orch._dispatch_project(pid)
    orch._dispatch_project(pid)

    entries = state.logger.read_log("project", pid, None)
    assert not any(entry["event"] == "diamond_reason_skipped" for entry in entries)
    with orch._lock:
        orch._members.pop(pid, None)
    assert intent.id
    orch.shutdown()


def test_reap_finished_future_removes_crashed_task_index(state):
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    future = Future()
    future.set_exception(RuntimeError("boom"))
    orch = Orchestrator(state, max_workers=1)
    with orch._lock:
        orch._task_index[(pid, "i999")] = future

    orch._reap_finished_futures()

    with orch._lock:
        assert (pid, "i999") not in orch._task_index
    entries = state.logger.read_log("project", pid, None)
    assert any(entry["event"] == "member_task_crash" and entry["intent"] == "i999" for entry in entries)
    orch.shutdown()


def test_failed_member_is_backed_off_before_same_intent_is_dispatched(state):
    from backend.core.orchestrator import Orchestrator
    from backend.members.base_member import SolveResult

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
        intent = edge_store.create_intent(conn, pid, ["origin"], "inspect binary", "diamond")

    future = Future()
    future.set_result(
        SolveResult(
            status="failed",
            steps=4,
            error="model did not return a valid JSON action",
        )
    )
    orch = Orchestrator(state, max_workers=1)
    with orch._lock:
        orch._task_index[(pid, intent.id)] = future

    orch._reap_finished_futures()

    key = (pid, intent.id)
    assert orch._member_failure_counts[key] == 1
    assert orch._member_retry_not_before[key] > time.monotonic()
    assert orch.runtime_status(pid)["phase"] == "degraded"
    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))
        return True

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    assert launched == []

    orch._member_retry_not_before[key] = time.monotonic() - 1
    orch._dispatch_project(pid)
    assert launched == [(pid, "aventurine", intent.id, "web", False)]
    entries = state.logger.read_log("project", pid, None)
    assert any(
        entry["event"] == "member_task_retry_scheduled"
        and entry["delay_seconds"] == 10
        for entry in entries
    )
    orch.shutdown()


def test_non_retryable_member_failure_is_not_requeued(state):
    from backend.core.orchestrator import Orchestrator
    from backend.members.base_member import SolveResult

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
        intent = edge_store.create_intent(conn, pid, ["origin"], "invalid terminal action", "diamond")

    future = Future()
    future.set_result(
        SolveResult(
            status="failed",
            steps=1,
            error="project is solved without a completion edge",
            retryable=False,
            error_kind="terminal_validation",
        )
    )
    orch = Orchestrator(state, max_workers=1)
    key = (pid, intent.id)
    with orch._lock:
        orch._task_index[key] = future
        orch._member_failure_counts[key] = 1
        orch._member_retry_not_before[key] = time.monotonic() + 60

    orch._reap_finished_futures()

    assert key not in orch._member_failure_counts
    assert key not in orch._member_retry_not_before
    entries = state.logger.read_log("project", pid, None)
    assert any(
        entry["event"] == "member_task_failed"
        and entry["error_kind"] == "terminal_validation"
        and entry["retryable"] is False
        for entry in entries
    )
    assert not any(entry["event"] == "member_task_retry_scheduled" for entry in entries)
    orch.shutdown()


def test_repeated_stalls_defer_original_intent_and_dispatch_alternative(state):
    from backend.core.orchestrator import IntentLease, Orchestrator
    from backend.members.base_member import SolveResult

    pid = _make_project(state, "web")
    with state.db.connect() as conn:
        graph_store.set_status(conn, pid, "running")
        stalled = edge_store.create_intent(
            conn, pid, ["origin"], "repeat a stalled scan", "diamond"
        )
        alternative = edge_store.create_intent(
            conn, pid, ["origin"], "run the alternate static query", "diamond"
        )

    orch = Orchestrator(state, max_workers=1)
    for _ in range(orch._MAX_CONSECUTIVE_STALLS):
        with state.db.connect() as conn:
            token = edge_store.claim_intent(conn, pid, stalled.id, "aventurine")
            assert token
        with orch._lock:
            orch._intent_leases[(pid, stalled.id)] = IntentLease(
                worker="aventurine", owner="aventurine", token=token
            )
        future = Future()
        future.set_result(SolveResult(status="stalled", steps=3))
        with orch._lock:
            orch._task_index[(pid, stalled.id)] = future
        orch._reap_finished_futures()

    key = (pid, stalled.id)
    assert orch._member_stall_counts[key] == orch._MAX_CONSECUTIVE_STALLS
    assert orch._member_retry_blocked(pid, stalled.id)
    with state.db.connect() as conn:
        assert edge_store.get_intent(conn, pid, stalled.id)["worker"] is None

    launched = []

    def capture_launch(project_id, member_name, intent_id, category, is_initial):
        launched.append((project_id, member_name, intent_id, category, is_initial))
        return True

    orch._launch_member = capture_launch
    orch._dispatch_project(pid)
    assert launched == [(pid, "aventurine", alternative.id, "web", False)]

    entries = state.logger.read_log("project", pid, None)
    assert any(
        entry["event"] == "member_intent_deferred"
        and entry["intent"] == stalled.id
        and entry["reason"] == "repeated_stalls"
        for entry in entries
    )
    orch.shutdown()


def test_legacy_block_logging_is_rate_limited(state):
    """T8: a hot legacy-block loop collapses to one line per backoff window.

    While a competition owns a project every legacy dispatch attempt is
    rejected; logging each one produced thousands of identical lines.  The
    first attempt logs, subsequent ones inside the window are suppressed, and
    the suppressed attempts surface as a running ``attempts`` count.
    """
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    orch = Orchestrator(state, max_workers=1)
    try:
        for _ in range(50):
            orch._log_legacy_block(pid, "jade", "intent-1")
        entries = [
            e for e in state.logger.read_log("project", pid, None)
            if e["event"] == "legacy_member_blocked_by_competition"
        ]
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1
        assert entries[0]["member"] == "jade"

        orch._log_legacy_block(pid, "topaz", "intent-2")
        entries = [
            e for e in state.logger.read_log("project", pid, None)
            if e["event"] == "legacy_member_blocked_by_competition"
        ]
        assert len(entries) == 2
    finally:
        orch.shutdown()


def test_engine_lease_conflict_logging_is_rate_limited(state):
    """T4: a stop-window engine-lease race collapses to one line per project.

    Several instances racing for the same project lease and losing used to log
    engine_lease_conflict on every tick, producing the storm T4 flags. The
    shared throttle emits the first loss, folds the rest into an attempt count,
    and tracks a second project independently.
    """
    from backend.core.orchestrator import Orchestrator

    pid = _make_project(state, "web")
    other = _make_project(state, "pwn")
    orch = Orchestrator(state, max_workers=1)
    try:
        for _ in range(40):
            orch._throttled_log(
                "engine_lease_conflict", pid, pid,
                requested_kind="competition", requested_owner="inst-a",
            )
        entries = [
            e for e in state.logger.read_log("project", pid, None)
            if e["event"] == "engine_lease_conflict"
        ]
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1

        orch._throttled_log(
            "engine_lease_conflict", other, other,
            requested_kind="competition", requested_owner="inst-a",
        )
        other_entries = [
            e for e in state.logger.read_log("project", other, None)
            if e["event"] == "engine_lease_conflict"
        ]
        assert len(other_entries) == 1
    finally:
        orch.shutdown()


def _link_project_to_competition_run(state, project_id, *, status, suffix):
    """Insert the minimal competition rows linking a project to a run status."""
    import json

    with state.db.connect() as conn:
        conn.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,status,created_at,updated_at)
               VALUES (%s,'test',%s,%s,%s,'confirmed',now(),now())
               ON CONFLICT (id) DO NOTHING""",
            (f"wf-{suffix}", f"wf {suffix}", json.dumps({}), f"dig-{suffix}"),
        )
        run_id = f"run-{suffix}"
        conn.execute(
            """INSERT INTO competition_runs
               (id,workflow_id,identity_key,idempotency_key,status,config_snapshot)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (run_id, f"wf-{suffix}", f"id-{suffix}", f"idem-{suffix}", status,
             json.dumps({})),
        )
        challenge_id = f"ch-{suffix}"
        conn.execute(
            """INSERT INTO competition_challenges
               (id,identity_key,external_id,run_id,project_id)
               VALUES (%s,%s,%s,%s,%s)""",
            (challenge_id, f"id-{suffix}", f"ext-{suffix}", run_id, project_id),
        )
        conn.execute(
            """INSERT INTO competition_run_challenges (run_id,challenge_id)
               VALUES (%s,%s)""",
            (run_id, challenge_id),
        )
    return run_id


def test_stopped_run_projects_are_not_resurrected_by_recovery(state):
    """T4: a project whose only competition run has stopped is terminal.

    The generic recovery loop must skip it: the coordinator already tore its
    seat down, so a lingering ``running`` graph status is stale and reviving a
    session would restart work the operator stopped. A project under a live run
    stays coordinator-owned (active), and a plain non-competition project is
    neither.
    """
    from backend.core.orchestrator import Orchestrator

    stopped_pid = _make_project(state, "web")
    active_pid = _make_project(state, "pwn")
    plain_pid = _make_project(state, "rev")
    _link_project_to_competition_run(state, stopped_pid, status="stopped", suffix="s")
    _link_project_to_competition_run(state, active_pid, status="running", suffix="a")

    orch = Orchestrator(state, max_workers=1)
    try:
        with state.db.connect() as conn:
            active, terminal = orch._competition_project_ownership(conn)
        assert stopped_pid in terminal
        assert active_pid in active
        assert stopped_pid not in active
        assert plain_pid not in active and plain_pid not in terminal
    finally:
        orch.shutdown()
