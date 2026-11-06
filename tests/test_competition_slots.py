"""Slot turnover: lookahead starts, reuse of a parked box, and async stops."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from backend.competition.service import CompetitionService
from backend.core.config import MEMBER_NAMES

NOW = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, one=None):
        self.one = one

    def fetchone(self):
        return self.one

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, log, one=None):
        self.log = log
        self.one = one

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        return _Cursor(self.one)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Db:
    def __init__(self, one=None):
        self.log: list = []
        self.one = one

    def connect(self):
        return _Connection(self.log, self.one)


class _Future:
    def __init__(self):
        self.calls = []

    def done(self):
        return False


def _service(*, count_row=None):
    db = _Db({"active": 0} if count_row is None else count_row)
    events: list = []
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(
        db=db,
        orchestrator=object(),
        config=SimpleNamespace(
            available_members=lambda: [SimpleNamespace(name=name) for name in MEMBER_NAMES]
        ),
    )
    service.store = SimpleNamespace(
        challenges=lambda run_id: [
            {"id": "c-1", "state": "ready", "metadata": {"remote": True}},
            {"id": "c-2", "state": "ready", "metadata": {"remote": True}},
            {"id": "c-3", "state": "ready", "metadata": {"remote": True}},
        ],
        assignments=lambda run_id, active_only=True: [],
        append_run_event=lambda run_id, kind, payload: events.append((kind, payload)),
    )
    service._platform_for_run = lambda run: SimpleNamespace(
        supports_instances=True, start_instance=lambda external_id: {"state": "ready"},
    )
    service._remote_instance_limit = lambda run, platform: 3
    service._run_ends_at = lambda run: None
    service._instance_ready = lambda challenge_id: False
    service._prestart_futures = {}
    future = _Future()

    class Pool:
        def submit(self, fn, *args, **kwargs):
            future.calls.append((fn, args))
            return future

    service._prestart_pool = Pool()
    service.prestart_future = future
    service.events = events
    service.db = db
    return service


def test_lookahead_warms_at_most_two_containers_without_assigning():
    service = _service()
    service._prestart_lookahead({"id": "run-1"})
    started = service.prestart_future.calls
    assert 1 <= len(started) <= 2
    assert all(call[0] == service._start_instance for call in started)
    assert [kind for kind, _ in service.events].count("instance.prestart") == len(started)
    # Only instance rows are written by the workers; no assignment happens here.
    assert not any("INSERT INTO competition_assignments" in sql for sql, _ in service.db.log)


def test_lookahead_does_nothing_when_the_platform_quota_is_full():
    service = _service(count_row={"active": 3})
    service._prestart_lookahead({"id": "run-1"})
    assert service.prestart_future.calls == []


def test_lookahead_skips_a_challenge_that_already_has_a_warm_instance():
    service = _service()
    service._instance_ready = lambda challenge_id: challenge_id == "c-1"
    service._prestart_lookahead({"id": "run-1"})
    warmed = {args[1]["id"] for _fn, args in service.prestart_future.calls}
    assert "c-1" not in warmed


def test_reuse_renews_a_stopped_container_before_starting_a_new_one():
    service = object.__new__(CompetitionService)
    events: list = []
    service.store = SimpleNamespace(
        append_run_event=lambda run_id, kind, payload: events.append((kind, payload))
    )
    db = _Db()
    service.state = SimpleNamespace(db=db)

    class Platform:
        def renew_instance(self, external_id):
            assert external_id == "3"
            return {"state": "ready", "entry": "10.0.0.1:31337"}

    reused = service._try_reuse_instance(
        {"id": "run-1"}, {"id": "c-1", "external_id": "3"},
        Platform(), {"state": "stopped"},
    )
    assert reused is True
    assert any("UPDATE competition_instances" in sql for sql, _ in db.log)
    assert events[-1][0] == "instance.reused"


def test_reuse_falls_back_to_start_when_renewal_fails():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(append_run_event=lambda *args: None)
    service.state = SimpleNamespace(db=_Db())

    class Platform:
        def renew_instance(self, external_id):
            raise RuntimeError("container is gone")

    assert service._try_reuse_instance(
        {"id": "run-1"}, {"id": "c-1", "external_id": "3"},
        Platform(), {"state": "stopped"},
    ) is False


def test_async_stop_marks_the_row_closing_before_it_returns():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(append_run_event=lambda *args: None)
    db = _Db()
    service.state = SimpleNamespace(db=db)
    service._platform_for_run = lambda run: SimpleNamespace()
    submitted: list = []

    class Pool:
        def submit(self, fn, *args, **kwargs):
            submitted.append((fn, args))
            return _Future()

    service._stop_pool = Pool()
    assert service._submit_stop({"id": "run-1"}, {"id": "c-1", "external_id": "3"}) is True
    assert submitted and submitted[0][1][1]["id"] == "c-1"
    assert any("state='stop_pending'" in sql for sql, _ in db.log)


def test_start_quota_guard_counts_live_containers():
    service = object.__new__(CompetitionService)
    db = _Db({"used": 3})
    service.state = SimpleNamespace(db=db)
    service._platform_for_run = lambda run: SimpleNamespace()
    service._remote_instance_limit = lambda run, platform: 3
    assert service._instance_slot_available(
        {"id": "run-1"}, {"id": "c-1"}, SimpleNamespace()
    ) is False
    db2 = _Db({"used": 2})
    service.state = SimpleNamespace(db=db2)
    assert service._instance_slot_available(
        {"id": "run-1"}, {"id": "c-1"}, SimpleNamespace()
    ) is True


def test_start_quota_guard_allows_when_the_limit_is_unknown():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(db=_Db())
    service._platform_for_run = lambda run: SimpleNamespace()
    service._remote_instance_limit = lambda run, platform: 0
    assert service._instance_slot_available(
        {"id": "run-1"}, {"id": "c-1"}, SimpleNamespace()
    ) is True
