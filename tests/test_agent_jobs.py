"""Invariants of detached sandbox jobs (P4a).

Two properties carry the feature and are the easy ones to get wrong:

* A job that outran the 60s foreground ceiling is still readable after the
  worker that started it has gone away, because the handle is a durable row
  plus files in the workspace volume rather than a docker exec id.
* ``lost`` is reported as ``lost``.  A process that vanished without recording
  an exit status must never be presented as a success, because a model told
  "exit 0" will build its next conclusion on work that may never have run.
"""
from __future__ import annotations

import os
import time

import pytest

from backend.agent.job_service import JobService
from backend.agent.jobs import AgentJobStore, JobConflict
from backend.agent.session import AgentSessionStore
from backend.persistence.database import Database
from backend.sandbox.sandbox import LocalSandbox

pytestmark = pytest.mark.postgres


@pytest.fixture
def db():
    database = Database().configure()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def session(db):
    return AgentSessionStore(db).create_session("competition", "competition:run:ch")


@pytest.fixture
def jobs(db):
    return AgentJobStore(db)


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def test_job_rows_record_lifecycle_and_keep_the_first_terminal_state(jobs, session):
    job = jobs.start(session["id"], "sleep 300", kind="bash", pid=4242)
    assert job["state"] == "running"
    assert job["exit_code"] is None

    jobs.note_progress(job["id"], stdout_offset=120)
    # Offsets only move forward: a re-read from an older offset is a valid
    # retry and must not rewind the cursor for the next incremental poll.
    assert jobs.note_progress(job["id"], stdout_offset=40)["stdout_offset"] == 120

    finished = jobs.finish(job["id"], "exited", exit_code=7)
    assert finished["exit_code"] == 7
    assert finished["finished_at"] is not None
    # A cancel racing the process's own exit must not overwrite the recorded
    # exit code with 'cancelled'.
    assert jobs.finish(job["id"], "cancelled")["state"] == "exited"
    assert jobs.finish(job["id"], "cancelled")["exit_code"] == 7

    with pytest.raises(JobConflict):
        jobs.finish(job["id"], "running")


def test_jobs_of_a_vanished_container_are_lost_not_exited(jobs, session):
    one = jobs.start(session["id"], "sleep 300")
    two = jobs.start(session["id"], "sleep 300")
    jobs.finish(two["id"], "exited", exit_code=0)

    lost = jobs.mark_lost_for_session(session["id"])
    assert [row["id"] for row in lost] == [one["id"]]
    assert jobs.job(one["id"])["state"] == "lost"
    # No exit code was invented for the job whose fate is unknown.
    assert jobs.job(one["id"])["exit_code"] is None
    assert jobs.job(two["id"])["state"] == "exited"


def test_detached_job_outlives_the_sixty_second_foreground_ceiling(
    jobs, session, tmp_path
):
    """The point of P4a: a command longer than the exec timeout can finish."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    # Shell builtins rather than an interpreter path: an absolute host path
    # would be refused by the very command policy this feature must respect.
    started = service.start("echo phase-1; sleep 1; echo phase-2; exit 3")
    assert started["ok"] is True

    job_id = started["job_id"]
    # Polling while it runs is non-blocking and reports progress, not an exit.
    assert service.status(job_id)["state"] in {"running", "exited"}

    final = wait_for(
        lambda: (lambda view: view if view["state"] == "exited" else None)(
            service.status(job_id)
        )
    )
    assert final is not None, "detached job never reached a terminal state"
    # The real exit code survives, rather than being flattened to ok/not-ok.
    assert final["exit_code"] == 3

    first = service.logs(job_id)
    assert "phase-1" in first["text"]
    # The stored cursor advances, so a second poll returns only new bytes
    # instead of re-reading what the model has already seen.
    assert service.logs(job_id)["text"] == ""
    assert service.logs(job_id, offset=0)["text"] == first["text"]


def test_job_logs_report_truncation_instead_of_hiding_it(jobs, session, tmp_path):
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("for i in $(seq 1 500); do echo 0123456789; done")
    assert started["ok"] is True
    job_id = started["job_id"]
    assert wait_for(
        lambda: service.status(job_id)["state"] == "exited"
    ), "job never exited"

    page = service.logs(job_id, limit=1000)
    assert page["truncated"] is True
    assert len(page["text"]) == 1000
    assert page["next_offset"] == 1000
    # Continuing from the cursor reaches the rest rather than restarting.
    assert service.logs(job_id, limit=1000)["offset"] == 1000


def test_job_start_applies_the_same_host_path_policy_as_bash(jobs, session, tmp_path):
    """A long job must not be a way around the command policy."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])

    blocked = service.start("cat /host/etc/shadow")
    assert blocked["ok"] is False
    assert "blocked" in blocked["error"]
    # Nothing was started and no row was left behind to poll.
    assert jobs.jobs(session["id"]) == []


def test_cancelling_a_job_kills_it_and_records_the_cancellation(
    jobs, session, tmp_path
):
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("sleep 120")
    job_id = started["job_id"]
    assert started["ok"] is True
    assert service.status(job_id)["state"] == "running"

    assert service.cancel(job_id)["ok"] is True
    assert jobs.job(job_id)["state"] == "cancelled"
    assert jobs.job(job_id)["finished_at"] is not None
    # Cancelling an already finished job is reported, not retried.
    assert service.cancel(job_id)["note"] == "job had already finished"


def test_a_job_whose_process_disappeared_is_reported_as_unknown(
    jobs, session, tmp_path
):
    """The distinction the model depends on: unknown is not success."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    job = jobs.start(session["id"], "sleep 300", pid=0)
    # Simulate the container having restarted: a row still marked running, a
    # pid file naming a process that is gone, and no recorded exit status.
    directory = sandbox._job_dir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pid").write_text("2147483646", encoding="utf-8")

    view = service.status(job["id"])
    assert view["state"] == "lost"
    assert view["exit_code"] is None
    assert "not" in view["warning"] and "success" in view["warning"]


def test_jobs_are_scoped_to_their_own_session(jobs, db, session):
    other = AgentSessionStore(db).create_session(
        "competition", "competition:run:other"
    )
    job = jobs.start(other["id"], "sleep 300")
    service = JobService(jobs, LocalSandbox("member", os.getcwd()), session["id"])

    for view in (
        service.status(job["id"]),
        service.logs(job["id"]),
        service.cancel(job["id"]),
    ):
        assert view["ok"] is False
        assert view["error"] == "job belongs to another session"


def test_stopping_a_run_resolves_its_jobs_as_lost_not_exited(db, tmp_path):
    """A container teardown leaves jobs whose fate was never recorded.

    Reporting them as ``exited 0`` would tell a reader the brute force
    finished successfully when it was actually killed with the container.
    """
    from backend.competition.store import CompetitionStore

    store = CompetitionStore(db)
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,created_at,updated_at)
               VALUES ('wf-jobs','test','test','{}','digest',now(),now())"""
        )
    run = store.create_run("wf-jobs", "platform/game/team", "jobs-start", {})
    run = store.transition_run(run["id"], "importing", revision=run["revision"])
    run = store.transition_run(run["id"], "running", revision=run["revision"])
    challenge = store.register_challenge(run["id"], "job-orphan", {})
    assert store.mark_ready(challenge["id"])
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")

    jobs = AgentJobStore(db)
    running_job = jobs.start(assignment["session_id"], "hashcat -a0 hash list")
    done_job = jobs.start(assignment["session_id"], "echo quick")
    jobs.finish(done_job["id"], "exited", exit_code=0)

    lost = jobs.mark_lost_for_session(assignment["session_id"])
    assert [row["id"] for row in lost] == [running_job["id"]]
    assert jobs.job(running_job["id"])["state"] == "lost"
    assert jobs.job(running_job["id"])["exit_code"] is None
    # An already finished job keeps its real outcome.
    assert jobs.job(done_job["id"])["state"] == "exited"
    assert jobs.job(done_job["id"])["exit_code"] == 0


def test_health_view_turn_aggregation_tracks_only_the_in_flight_turn(db, session):
    """A finished turn must not look in-flight forever.

    ``current_turn_started_at`` filters on status='running'. Without that
    filter a completed session would report a turn in flight indefinitely and
    its quiet timer would never advance, hiding a real stall.
    """
    store = AgentSessionStore(db)
    store.claim_writer(session["id"], "competition-assignment", "w1")

    store.start_turn(session["id"], 1, "w1", 1)
    store.finish_turn(session["id"], 1, "completed", usage={"prompt_tokens": 10})
    first = store.health(session["id"])
    assert first["current_turn"] == 1
    assert first["turn_status"] == "completed"
    assert first["current_turn_started_at"] is None

    store.start_turn(session["id"], 2, "w1", 1)
    live = store.health(session["id"])
    assert live["current_turn"] == 2
    assert live["turn_status"] == "running"
    assert live["current_turn_started_at"] is not None

    store.finish_turn(session["id"], 2, "completed")
    assert store.health(session["id"])["current_turn_started_at"] is None


def test_a_long_in_flight_turn_does_not_accumulate_quiet_time(db, session):
    """The case that must not be misread: a slow provider turn is progress.

    A provider read timeout alone is 300s. If quiet time grew while a turn was
    in flight, the watchdog would cancel work that was simply slow.
    """
    from backend.agent.watchdog import classify

    store = AgentSessionStore(db)
    store.claim_writer(session["id"], "competition-assignment", "w1")
    with db.connect() as connection:
        connection.execute(
            "UPDATE agent_sessions SET created_at=now()-interval '1 hour' WHERE id=%s",
            (session["id"],),
        )
        connection.execute(
            """UPDATE agent_session_writers
               SET last_renewed_at=now()-interval '1 hour' WHERE session_id=%s""",
            (session["id"],),
        )
    store.start_turn(session["id"], 1, "w1", 1)

    fresh = store.health(session["id"])
    assert fresh["quiet_seconds"] < 60
    assert classify(fresh).level == "ok"

    # But a turn that has been 'running' for an hour with no events is a real
    # stall, and must escalate rather than be excused as "slow".
    with db.connect() as connection:
        connection.execute(
            """UPDATE agent_turns SET started_at=now()-interval '1 hour'
               WHERE session_id=%s""",
            (session["id"],),
        )
    wedged = store.health(session["id"])
    assert wedged["quiet_seconds"] > 3000
    assert classify(wedged).level == "handoff"


def test_incremental_log_reads_tile_the_output_exactly(jobs, session, tmp_path):
    """Repeated polls must neither lose bytes nor repeat them.

    A model polling a long job reasons from the concatenation of what it read.
    A gap silently drops evidence; an overlap makes it count the same finding
    twice. An explicit offset is a seek, so it must not disturb the cursor the
    default path uses.
    """
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("for i in $(seq 1 200); do echo LINE$i; done")
    job_id = started["job_id"]
    assert wait_for(lambda: service.status(job_id)["state"] == "exited")

    chunks = []
    while True:
        page = service.logs(job_id, limit=300)
        if not page["text"]:
            break
        chunks.append(page["text"])
    written = (sandbox._job_dir(job_id) / "stdout").read_bytes().decode()
    assert len(chunks) > 1, "limit was too large to exercise paging"
    assert "".join(chunks) == written

    before = jobs.job(job_id)["stdout_offset"]
    service.logs(job_id, offset=0, limit=50)
    assert jobs.job(job_id)["stdout_offset"] == before
