from __future__ import annotations

import pytest

from backend.blackboard import graph_store
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.state import AppState
from backend.core.verdict_worker import VerdictWorker
from backend.platform.ret2shell import Ret2ShellRateLimitError
from tests.helpers import write_mock_config


class FakeClient:
    """Scripted platform client: returns queued submit results or raises."""

    def __init__(self, results=None, *, already_solved=False, error=None):
        self.results = list(results or [])
        self.already_solved = already_solved
        self.error = error
        self.submitted: list[str] = []

    def challenge_status(self, challenge_id):
        return {"solved": self.already_solved}

    def submit_flag(self, challenge_id, flag, *, check_solved=True):
        if self.error is not None:
            raise self.error
        self.submitted.append(flag)
        if self.results:
            return self.results.pop(0)
        return {"id": 1, "solved": True, "result": "Correct"}


@pytest.fixture
def state(tmp_path):
    cfgdir = write_mock_config(tmp_path / "config")
    st = AppState(root=tmp_path, config_dir=cfgdir)
    st.pool.backend = "local"
    st.network.backend = "local"
    return st


def _pending_project(state, flag="moectf{answer}"):
    with state.db.connect() as conn:
        pid = graph_store.create_project(
            conn, "Demo", "origin", "goal", "web", external_id="123"
        )
        graph_store.set_flag(conn, pid, flag)
        graph_store.set_status(conn, pid, "pending_verdict")
    return pid


def _worker(state, client) -> VerdictWorker:
    return VerdictWorker(state, client_factory=lambda: client)


def test_lifecycle_pending_verdict_transitions(state):
    pid = _pending_project(state)
    lc = Lifecycle(state.db)
    lc.transition(pid, "completed")
    assert lc.status(pid) == "completed"
    with pytest.raises(LifecycleError):
        lc.transition(pid, "running")


def test_accepted_verdict_completes_project(state):
    client = FakeClient([{"id": 7, "solved": True, "result": "Correct"}])
    pid = _pending_project(state)
    _worker(state, client).process_pending()
    assert Lifecycle(state.db).status(pid) == "completed"
    with state.db.connect() as conn:
        sub = graph_store.get_submission(conn, pid, "moectf{answer}")
        broadcasts = graph_store.list_broadcasts(conn)
    assert sub["status"] == "solved"
    assert sub["submission_id"] == "7"
    assert any(b.project_id == pid for b in broadcasts)


def test_already_solved_on_platform_completes_without_submission(state):
    client = FakeClient(already_solved=True)
    pid = _pending_project(state)
    _worker(state, client).process_pending()
    assert Lifecycle(state.db).status(pid) == "completed"
    assert client.submitted == []


def test_rejected_verdict_reopens_with_feedback(state):
    pid = _pending_project(state)
    # Stale conclusions that must be purged on reopen, and a blacklist entry
    # that must survive it.
    state.memory.add(
        "exploit", "old wrong solution", "exploit: Flag: moectf{answer}",
        project_id=pid, source="diamond",
    )
    state.memory.add(
        "lessons", "older rejection", "Platform judge REJECTED the flag 'moectf{old}'",
        tags=["rejected-flag"], project_id=pid, source="verdict",
    )
    client = FakeClient([{"id": 3, "solved": False, "result": "Wrong flag"}])
    _worker(state, client).process_pending()

    assert Lifecycle(state.db).status(pid) == "stopped"
    with state.db.connect() as conn:
        sub = graph_store.get_submission(conn, pid, "moectf{answer}")
        hints = graph_store.list_hints(conn, pid)
    assert sub["status"] == "rejected"
    assert any("moectf{answer}" in h.content for h in hints)

    memories = state.memory.list(None)
    project_mems = [m for m in memories if m.project_id == pid]
    # stale exploit conclusion purged; blacklist kept and new rejection added
    assert not any(m.category == "exploit" for m in project_mems)
    rejected = [m for m in project_mems if "rejected-flag" in m.tags]
    assert len(rejected) == 2


def test_new_flag_not_blocked_by_old_rejection(state):
    """The dedup key is (project, flag): a re-derived flag submits normally."""
    pid = _pending_project(state, flag="moectf{wrong}")
    client = FakeClient([{"id": 1, "solved": False, "result": "Wrong"}])
    worker = _worker(state, client)
    worker.process_pending()
    assert Lifecycle(state.db).status(pid) == "stopped"

    client.results.append({"id": 2, "solved": True, "result": "Correct"})
    with state.db.connect() as conn:
        graph_store.set_flag(conn, pid, "moectf{right}")
        graph_store.set_status(conn, pid, "pending_verdict")
    worker.process_pending()
    assert Lifecycle(state.db).status(pid) == "completed"
    with state.db.connect() as conn:
        assert graph_store.get_submission(conn, pid, "moectf{wrong}")["status"] == "rejected"
        assert graph_store.get_submission(conn, pid, "moectf{right}")["status"] == "solved"
    assert client.submitted == ["moectf{wrong}", "moectf{right}"]


def test_rate_limit_keeps_task_pending(state):
    """HTTP 429 / local quota: the pending submission is never dropped."""
    client = FakeClient(error=Ret2ShellRateLimitError("quota exhausted"))
    pid = _pending_project(state)
    worker = _worker(state, client)
    assert worker.process_one(pid) is None
    assert Lifecycle(state.db).status(pid) == "pending_verdict"
    with state.db.connect() as conn:
        sub = graph_store.get_submission(conn, pid, "moectf{answer}")
    assert sub["status"] == "pending"

    # After the backoff the same task is retried and can succeed.
    client.error = None
    worker._retry_not_before.clear()
    worker.process_pending()
    assert Lifecycle(state.db).status(pid) == "completed"


def test_judge_timeout_retries_then_reopens(state):
    state.config.runtime.verdict_max_attempts = 2
    client = FakeClient([{"id": 1, "solved": None, "result": ""}] * 5)
    pid = _pending_project(state)
    worker = _worker(state, client)

    first = worker.submit_and_apply(pid, "moectf{answer}", retry_backoff=False)
    assert first == {"solved": None, "verdict": "pending"}
    assert Lifecycle(state.db).status(pid) == "pending_verdict"

    second = worker.submit_and_apply(pid, "moectf{answer}", retry_backoff=False)
    assert second == {"solved": None, "verdict": "unknown"}
    assert Lifecycle(state.db).status(pid) == "stopped"
    with state.db.connect() as conn:
        assert graph_store.get_submission(conn, pid, "moectf{answer}")["status"] == "unknown"


def test_no_platform_client_leaves_pending(state):
    pid = _pending_project(state)
    worker = VerdictWorker(state, client_factory=lambda: None)
    worker.process_pending()
    assert Lifecycle(state.db).status(pid) == "pending_verdict"
