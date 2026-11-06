"""The two halves of "stop when the task is over".

The 2026-10-25 run kept three containers and a rate-limited model loop alive
for five hours after the platform had already stopped scoring.  These tests pin
the two mechanisms that make that impossible: a declared end that drains the
run, and a platform-closed signal that cannot be swallowed by the generic sync
retry.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.competition.service import CompetitionService
from backend.pentest import guard

NOW = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)


def bare_service(**attrs):
    service = object.__new__(CompetitionService)
    service._closed_runs = {}
    service.deadline_grace_seconds = 120
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


def run_row(**overrides):
    run = {
        "id": "run-1",
        "status": "running",
        "revision": 3,
        "created_at": NOW.isoformat(),
        "config_snapshot": {"workflow_spec": {}},
    }
    run.update(overrides)
    return run




def test_ends_at_from_an_explicit_timestamp():
    ends = NOW + timedelta(hours=6)
    service = bare_service()
    assert service._run_ends_at(
        run_row(config_snapshot={"workflow_spec": {"ends_at": ends.isoformat()}})
    ) == ends


def test_ends_at_from_the_task_window():
    """A benchmark task has no timestamp of its own, only a duration."""
    service = bare_service()
    assert service._run_ends_at(
        run_row(config_snapshot={"workflow_spec": {"duration_minutes": 360}})
    ) == NOW + timedelta(hours=6)


def test_no_declared_end_leaves_the_watchdog_idle():
    assert bare_service()._run_ends_at(run_row()) is None


def test_unparseable_duration_is_ignored():
    service = bare_service()
    run = run_row(config_snapshot={"workflow_spec": {"duration_minutes": "soon"}})
    assert service._run_ends_at(run) is None




def test_zero_duration_disables_the_local_guard():
    service = bare_service()
    run = run_row(config_snapshot={"workflow_spec": {"duration_minutes": 0}})
    assert service._run_ends_at(run) is None


def test_config_guard_applies_only_when_the_spec_has_no_window(monkeypatch):
    service = bare_service()
    service.state = SimpleNamespace(
        config=SimpleNamespace(runtime=SimpleNamespace(run_deadline_guard_minutes=60))
    )
    assert service._run_ends_at(run_row()) == NOW + timedelta(hours=1)
    explicit = run_row(config_snapshot={"workflow_spec": {"duration_minutes": 180}})
    assert service._run_ends_at(explicit) == NOW + timedelta(hours=3)


def test_a_declared_end_is_never_shortened_by_a_local_guard():
    ends = NOW + timedelta(hours=6)
    service = bare_service()
    run = run_row(config_snapshot={"workflow_spec": {
        "ends_at": ends.isoformat(), "duration_minutes": 180,
    }})
    assert service._run_ends_at(run) == ends


def _window_service():
    executed = []
    events = []

    class Cursor:
        def fetchone(self):
            return {"value": 1}

    class Connection:
        def execute(self, sql, params=None):
            executed.append((" ".join(str(sql).split())[:80], params))
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Db:
        def connect(self):
            return Connection()

    service = bare_service()
    service.state = SimpleNamespace(db=Db())
    service.store = SimpleNamespace(
        append_run_event=lambda run_id, kind, payload: events.append((kind, payload)),
    )
    return service, executed, events


def test_a_short_local_guard_is_promoted_to_the_declared_window():
    service, executed, events = _window_service()
    ends = NOW + timedelta(hours=6)
    run = run_row(config_snapshot={"workflow_spec": {
        "ends_at": ends.isoformat(), "duration_minutes": 180,
    }})
    service._validate_run_window(run)
    assert events[0][0] == "run.window_mismatch"
    payload = events[0][1]
    assert payload["local_guard_minutes"] == 180
    assert payload["window_minutes"] == 360
    assert payload["promoted_to_minutes"] == 360
    assert run["config_snapshot"]["workflow_spec"]["duration_minutes"] == 360
    assert any("jsonb_set" in sql for sql, _ in executed)


def test_a_guard_at_least_as_long_as_the_window_is_left_alone():
    service, _executed, events = _window_service()
    ends = NOW + timedelta(hours=6)
    run = run_row(config_snapshot={"workflow_spec": {
        "ends_at": ends.isoformat(), "duration_minutes": 360,
    }})
    service._validate_run_window(run)
    assert events == []


@pytest.mark.parametrize("reason", ["ended", "closed"])
def test_ended_and_closed_are_terminal(reason):
    error = RuntimeError("task already finished")
    error.reason = reason
    assert CompetitionService._is_platform_closed(error)


@pytest.mark.parametrize("reason", ["transient", "quota", "rate_limited", "missing", ""])
def test_retryable_reasons_are_not_terminal(reason):
    error = RuntimeError("try again")
    error.reason = reason
    assert not CompetitionService._is_platform_closed(error)


def test_a_closed_run_is_handed_to_the_teardown():
    service = bare_service()
    error = RuntimeError("409 invalid_state: task already finished")
    error.reason = "ended"
    service._note_platform_closed("run-1", error)
    assert "run-1" in service._closed_runs


def test_teardown_failure_keeps_the_signal_for_the_next_tick():
    """A failed close must be retried, not lost: the containers stay up."""

    service = bare_service()
    service._closed_runs = {"run-1": "boom"}
    service.store = SimpleNamespace(get_run=lambda run_id: run_row(status="running"))

    def explode(run, **_kwargs):
        raise RuntimeError("teardown fault")

    service._close_run = explode
    service._flush_platform_closed()
    assert "run-1" in service._closed_runs


def test_a_finished_run_is_not_closed_twice():
    service = bare_service()
    service._closed_runs = {"run-1": "boom"}
    service.store = SimpleNamespace(get_run=lambda run_id: run_row(status="finished"))
    calls = []
    service._close_run = lambda run, **kwargs: calls.append(run)
    service._flush_platform_closed()
    assert calls == []
    assert service._closed_runs == {}




def test_writeup_hits_warn_by_default(monkeypatch):
    monkeypatch.delenv(guard.ANSWER_MODE_ENV, raising=False)
    hits = guard.scan('curl "https://example.com/search?q=demo writeup"')
    assert any(hit["kind"] == "answers" for hit in hits)
    assert guard.blocking_hits(hits) == []


def test_writeup_hits_block_when_the_operator_opts_in(monkeypatch):
    monkeypatch.setenv(guard.ANSWER_MODE_ENV, "block")
    hits = guard.scan('curl "https://example.com/search?q=demo writeup"')
    assert guard.blocking_hits(hits)


def test_touching_the_platform_always_blocks(monkeypatch):
    monkeypatch.delenv(guard.ANSWER_MODE_ENV, raising=False)
    hits = guard.scan("curl https://tsecbench.zc.tencent.com/openapi/v1/challenges")
    assert [hit["kind"] for hit in guard.blocking_hits(hits)] == ["platform"]
