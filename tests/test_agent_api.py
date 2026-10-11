from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.agent.jobs import AgentJobStore
from backend.agent.session import AgentSessionStore
from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as c:
        setup_test_auth(c)
        c.app.state.ipc.config_dir = cfgdir
        c.app.state.ipc.reload_config()
        yield c


@pytest.fixture
def sessions(client):
    return AgentSessionStore(client.app.state.ipc.db)


def owned(store, task_key="competition:run:ch", owner="worker-a"):
    session = store.create_session("competition", task_key)
    store.claim_writer(session["id"], "competition-assignment", owner)
    return session


def test_session_list_reports_each_signal_separately(client, sessions):
    session = owned(sessions)
    sessions.append_event(
        session["id"], "call-1", "tool_call", {"name": "bash"},
        owner="worker-a", epoch=1,
    )

    response = client.get("/api/agent/sessions?kind=competition")
    assert response.status_code == 200
    rows = response.json()["sessions"]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == session["id"]
    assert row["worker_live"] is True
    assert row["event_count"] == 1
    assert row["last_event_kind"] == "tool_call"
    assert row["quiet_seconds"] >= 0
    assert row["health"] == "ok"
    assert row["health_reason"]


def test_session_list_is_filtered_by_scope(client, sessions):
    owned(sessions, task_key="competition:run:one", owner="worker-one")
    owned(sessions, task_key="competition:run:two", owner="worker-two")

    scoped = client.get(
        "/api/agent/sessions?task_key=competition:run:one"
    ).json()["sessions"]
    assert [row["task_key"] for row in scoped] == ["competition:run:one"]

    assert client.get("/api/agent/sessions?kind=ops").json()["sessions"] == []


def test_session_detail_surfaces_running_jobs_next_to_health(client, sessions):
    """A healthy session can look idle for hours; the job explains why."""
    session = owned(sessions)
    jobs = AgentJobStore(client.app.state.ipc.db)
    jobs.start(session["id"], "hashcat -a0 hash wordlist", pid=4242)

    body = client.get(f"/api/agent/sessions/{session['id']}").json()
    assert body["session"]["id"] == session["id"]
    assert body["health"]["running_jobs"] == 1
    assert [job["command"] for job in body["jobs"]] == [
        "hashcat -a0 hash wordlist"
    ]

    listed = client.get(
        f"/api/agent/sessions/{session['id']}/jobs?active_only=true"
    ).json()["jobs"]
    assert len(listed) == 1
    assert listed[0]["state"] == "running"


def test_a_quiet_session_with_a_running_job_is_not_reported_as_stalled(
    client, sessions
):
    """P4a's long work must not look like P5c's failure.

    A brute force that polls its job is progressing; grading it as stalled
    would re-impose the ceiling the job tools exist to remove.
    """
    session = owned(sessions)
    jobs = AgentJobStore(client.app.state.ipc.db)
    jobs.start(session["id"], "sleep 100000", pid=1)
    with client.app.state.ipc.db.connect() as connection:
        connection.execute(
            """UPDATE agent_sessions SET created_at=now()-interval '2 hours'
               WHERE id=%s""",
            (session["id"],),
        )
        connection.execute(
            """UPDATE agent_session_writers
               SET last_renewed_at=now()-interval '2 hours'
               WHERE session_id=%s""",
            (session["id"],),
        )
        connection.execute(
            """UPDATE agent_jobs SET last_progress_at=now()-interval '2 hours'
               WHERE session_id=%s""",
            (session["id"],),
        )

    row = client.get("/api/agent/sessions?kind=competition").json()["sessions"][0]
    assert row["quiet_seconds"] > 3600
    assert row["health"] == "diagnose"
    assert "job(s) still running" in row["health_reason"]


def test_session_detail_404s_for_an_unknown_session(client):
    assert client.get("/api/agent/sessions/does-not-exist").status_code == 404


def test_session_event_stream_rejects_a_malformed_cursor(client, sessions):
    session = owned(sessions)
    response = client.get(
        f"/api/agent/sessions/{session['id']}/events",
        headers={"Last-Event-ID": "not-a-number"},
    )
    assert response.status_code == 400


def test_session_event_stream_404s_for_an_unknown_session(client):
    response = client.get("/api/agent/sessions/nope/events")
    assert response.status_code == 404


def test_scope_events_read_across_sessions_in_one_pass(client, sessions):
    """The list view needs every session's latest activity, not N requests."""
    first = owned(sessions, task_key="competition:run:a", owner="worker-a")
    second = owned(sessions, task_key="competition:run:b", owner="worker-b")
    sessions.append_event(
        first["id"], "e1", "tool_call", {"n": 1}, owner="worker-a", epoch=1
    )
    sessions.append_event(
        second["id"], "e2", "tool_call", {"n": 2}, owner="worker-b", epoch=1
    )
    sessions.append_event(
        first["id"], "e3", "tool_result", {"n": 3}, owner="worker-a", epoch=1
    )

    rows = sessions.events_for_scope_after_event_id(0, kind="competition")
    assert [row["event_key"] for row in rows] == ["e1", "e2", "e3"]
    assert {row["session_id"] for row in rows} == {first["id"], second["id"]}

    assert sessions.events_for_scope_after_event_id(
        rows[-1]["event_id"], kind="competition"
    ) == []


def test_scope_events_rejects_a_malformed_cursor(client):
    response = client.get(
        "/api/agent/events", headers={"Last-Event-ID": "nope"}
    )
    assert response.status_code == 400
