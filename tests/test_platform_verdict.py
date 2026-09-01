"""Platform-verdict gate: ret2shell-linked projects are solved only after the
platform judge confirms the candidate flag."""

from __future__ import annotations

import time

import pytest

from backend.blackboard import edge_store, graph_store
from backend.core.ipc import submit_flag_candidate
from backend.core.lifecycle import Lifecycle
from backend.core.orchestrator import Orchestrator
from backend.core.state import AppState
from backend.platform.ret2shell import Ret2ShellRateLimitError
from tests.helpers import write_mock_config


@pytest.fixture
def state(tmp_path):
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


class FakeR2SClient:
    """Scripted stand-in for Ret2ShellClient.submit_flag."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[int, str]] = []

    def submit_flag(self, challenge_id, flag, game_id=None, *, check_solved=True):
        del game_id, check_solved
        self.calls.append((challenge_id, flag))
        outcome = self.outcomes.pop(0) if self.outcomes else {"solved": True, "result": "accepted"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_r2s_project(state) -> str:
    with state.db.connect() as conn:
        pid = graph_store.create_project(
            conn,
            "R2S Demo",
            "https://platform.example/challenge",
            "capture the flag",
            "web",
            external_id="42",
            platform="ret2shell",
        )
        graph_store.set_status(conn, pid, "running")
    return pid


def _raise_candidate(state, pid: str, flag: str) -> dict:
    with state.db.connect() as conn:
        edge_store.complete_goal_intent(conn, pid, ["origin"], "flag captured", "tester", "tester")
        return submit_flag_candidate(conn, pid, flag, source="test")


def _wait_status(state, pid: str, targets: set[str], timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    status = Lifecycle(state.db).status(pid)
    while time.monotonic() < deadline:
        status = Lifecycle(state.db).status(pid)
        if status in targets:
            return status
        time.sleep(0.05)
    return status


def _submissions(state, pid: str) -> list[dict]:
    with state.db.connect() as conn:
        return conn.execute(
            "SELECT * FROM flag_submissions WHERE project_id = %s ORDER BY id",
            (pid,),
        ).fetchall()


def test_candidate_is_queued_not_accepted_before_verdict(state):
    pid = _make_r2s_project(state)
    result = _raise_candidate(state, pid, "flag{demo}")
    assert result["mode"] == "pending"
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, pid)
    assert row["status"] == "running"
    assert row["flag"] is None
    submissions = _submissions(state, pid)
    assert [s["status"] for s in submissions] == ["pending"]


def test_platform_verdict_accepts_correct_flag(state):
    client = FakeR2SClient([{"solved": True, "result": "correct"}])
    state.ret2shell_client = client
    pid = _make_r2s_project(state)
    _raise_candidate(state, pid, "flag{demo}")

    orch = Orchestrator(state, max_workers=2)
    try:
        orch.on_flag_found(pid)
        assert _wait_status(state, pid, {"solved"}) == "solved"
    finally:
        orch.shutdown()

    assert client.calls == [(42, "flag{demo}")]
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, pid)
        broadcasts = graph_store.list_broadcasts(conn)
    assert row["flag"] == "flag{demo}"
    assert row["flag_verified_at"] is not None
    assert any(b.project_id == pid for b in broadcasts)
    submissions = _submissions(state, pid)
    assert [s["status"] for s in submissions] == ["verified"]


def test_platform_verdict_rejects_wrong_flag_and_resumes(state):
    client = FakeR2SClient([
        {"solved": False, "result": "wrong flag"},
        {"solved": True, "result": "correct"},
    ])
    state.ret2shell_client = client
    pid = _make_r2s_project(state)
    _raise_candidate(state, pid, "flag{wrong}")

    orch = Orchestrator(state, max_workers=2)
    try:
        orch.on_flag_found(pid)
        # rejected -> no more pending candidates -> back to running
        assert _wait_status(state, pid, {"running"}) == "running"
        submissions = _submissions(state, pid)
        assert [s["status"] for s in submissions] == ["rejected"]
        assert submissions[0]["error"] == "wrong flag"
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, pid)
        assert row["flag"] is None

        # The rejection is written to experience memory so members do not
        # resubmit the same string.
        rejected = [
            m for m in state.memory.list()
            if m.project_id == pid and "rejected-flag" in m.tags
        ]
        assert len(rejected) == 1
        assert "'flag{wrong}'" in rejected[0].content

        # A corrected candidate is judged and accepted.
        _raise_candidate(state, pid, "flag{right}")
        orch.on_flag_found(pid)
        assert _wait_status(state, pid, {"solved"}) == "solved"
    finally:
        orch.shutdown()

    assert client.calls == [(42, "flag{wrong}"), (42, "flag{right}")]
    statuses = [s["status"] for s in _submissions(state, pid)]
    assert statuses == ["rejected", "verified"]


def test_platform_verdict_unknown_is_retried_then_parked(state):
    client = FakeR2SClient([
        Ret2ShellRateLimitError("quota exhausted"),
        Ret2ShellRateLimitError("quota exhausted"),
        Ret2ShellRateLimitError("quota exhausted"),
    ])
    state.ret2shell_client = client
    pid = _make_r2s_project(state)
    _raise_candidate(state, pid, "flag{demo}")

    orch = Orchestrator(state, max_workers=2)
    try:
        for attempt in (1, 2, 3):
            orch.on_flag_found(pid)
            # wait until the async verdict job released the project guard
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with orch._lock:
                    busy = pid in orch._completing
                if not busy:
                    break
                time.sleep(0.05)
            assert pid not in orch._completing
            if attempt < 3:
                # still pending, waiting for the next scheduler tick
                assert _submissions(state, pid)[0]["status"] == "pending"
                assert Lifecycle(state.db).status(pid) == "flag_found"
        # attempts exhausted: parked as error, project resumes solving
        assert _submissions(state, pid)[0]["status"] == "error"
        assert Lifecycle(state.db).status(pid) == "running"
    finally:
        orch.shutdown()

    assert len(client.calls) == 3


def test_verdict_is_deferred_without_platform_client(state):
    state.ret2shell_client = None
    pid = _make_r2s_project(state)
    _raise_candidate(state, pid, "flag{demo}")

    orch = Orchestrator(state, max_workers=2)
    try:
        orch.on_flag_found(pid)
    finally:
        orch.shutdown()

    assert Lifecycle(state.db).status(pid) == "flag_found"
    assert _submissions(state, pid)[0]["status"] == "pending"
    entries = state.logger.read_log("project", pid, None)
    assert any(entry["event"] == "platform_verdict_deferred" for entry in entries)


def test_non_platform_project_is_still_accepted_locally(state):
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, "Local Demo", "origin", "capture the flag", "web")
        graph_store.set_status(conn, pid, "running")
    result = _raise_candidate(state, pid, "flag{local}")
    assert result["mode"] == "verified"
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, pid)
    assert row["status"] == "solved"
    assert row["flag"] == "flag{local}"
