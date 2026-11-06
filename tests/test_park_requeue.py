"""Park -> handoff -> requeue, without losing the session or the queue order.

The 2026-10-25 replay parked five targets and let two of them take a remote
slot again within minutes while fourteen challenges had never started.  These
tests pin the ordering, the cooldown floor and the handoff-before-ready rule.
"""

from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from backend.competition.policy import Challenge
from backend.competition.service import CompetitionService

NOW = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, log, row=None):
        self.log = log
        self.row = row

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        return _Cursor(self.row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Db:
    def __init__(self, log, row=None):
        self.log = log
        self.row = row

    def connect(self):
        return _Connection(self.log, self.row)


def _service(*, db_row=None, parked_rank=7):
    log: list = []
    events: list = []
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(db=_Db(log, db_row), orchestrator=None)
    service.store = SimpleNamespace(
        challenges=lambda run_id: [{
            "id": "ch-1", "project_id": "proj-1", "external_id": "a-18",
            "metadata": {"attempts": 1},
        }],
        release_challenge_assignments=lambda run_id, challenge_id: None,
        append_run_event=lambda run_id, kind, payload: events.append((kind, payload)),
    )
    service.per_challenge_minutes = 45
    service.park_digests_enabled = True
    service.park_digest_timeout = 0.05
    service.hint_at_attempt = 0
    service._digest_futures = {}
    service._platform_for_run = lambda run: None
    service._instance_ready = lambda challenge_id: False
    service._release_project_engine = lambda project_id: None
    service._maybe_buy_hint = lambda *args: None
    service._next_parked_rank = lambda run_id: parked_rank
    service.digest_calls = events
    return service, log, events


def test_park_writes_the_handoff_before_the_challenge_becomes_ready():
    service, log, events = _service()

    order: list[str] = []

    def digest(run, row, attempts, backoff):
        order.append("digest")
        return True

    service._queue_park_digest = digest
    real_execute = _Connection.execute

    def execute(self, sql, params=None):
        normalised = " ".join(str(sql).split())
        if "UPDATE competition_challenges" in normalised and "state='ready'" in normalised:
            order.append("ready")
        return real_execute(self, sql, params)

    _Connection.execute = execute
    try:
        parked = service._park_stalled_challenges(
            {"id": "run-1"},
            [Challenge(
                "ch-1", "web", NOW, remote=True, state="solving",
                members=("amber",), attempt_started_at=NOW - timedelta(minutes=46),
                attempts=0,
            )],
            NOW,
        )
    finally:
        _Connection.execute = real_execute

    assert parked == ["ch-1"]
    assert order == ["digest", "ready"]
    payload = next(payload for kind, payload in events if kind == "challenge.parked")
    assert payload["parked_rank"] == 7
    assert payload["retry_in_minutes"] == 5
    assert payload["digest_ready"] is True
    _ready_sql, ready_params = next(
        (sql, params) for sql, params in log
        if "UPDATE competition_challenges" in sql and "state='ready'" in sql
    )
    metadata = next(p for p in ready_params if isinstance(p, Jsonb)).obj
    assert metadata["parked_rank"] == 7
    assert metadata["attempts"] == 1
    assert metadata["park_digest_ready"] is True


def test_next_parked_rank_continues_after_the_highest_rank_in_the_run():
    service, _log, _events = _service(db_row={"max_rank": 4})
    service._next_parked_rank = CompetitionService._next_parked_rank.__get__(service)
    assert service._next_parked_rank("run-1") == 5


def test_digest_timeout_falls_back_to_the_template_and_reports_why():
    service, _log, events = _service()
    future: Future = Future()  # never completes
    service._digest_pool = SimpleNamespace(submit=lambda *args, **kwargs: future)
    written: list[str] = []
    service._write_park_fallback = (
        lambda run, row, attempts, backoff, *, reason="": written.append(reason) or True
    )
    service._latest_session_id = lambda run_id, challenge_id: ""
    service.store.events = lambda session_id, limit=200: []

    result = service._queue_park_digest(
        {"id": "run-1"}, {"id": "ch-1", "project_id": "proj-1"}, 2, 10,
    )

    assert result is True
    assert written == ["timeout"]
    assert any(kind == "challenge.summary_timeout" for kind, _ in events)


def test_a_disabled_digest_never_blocks_the_park():
    service, _log, _events = _service()
    service.park_digests_enabled = False
    assert service._queue_park_digest(
        {"id": "run-1"}, {"id": "ch-1", "project_id": "proj-1"}, 1, 5,
    ) is False


def test_primary_intent_reuses_the_open_intent_owned_by_the_member(monkeypatch):
    from backend.blackboard import graph_store

    detail = SimpleNamespace(
        intents=[SimpleNamespace(id="intent-old", to=None, worker="amber")],
        facts=[],
    )
    monkeypatch.setattr(graph_store, "project_detail", lambda conn, pid: detail)
    service, _log, events = _service()
    assert service._ensure_primary_intent(
        {"id": "run-1"}, {"id": "ch-1", "metadata": {"attempts": 2}},
        "proj-1", "amber",
    ) == "intent-old"
    assert events[-1] == (
        "assignment.primary_intent",
        {"challenge_id": "ch-1", "member": "amber",
         "intent_id": "intent-old", "source": "reused"},
    )


def test_primary_intent_creates_one_when_the_graph_has_none(monkeypatch):
    from backend.blackboard import edge_store, graph_store

    created: list = []
    detail = SimpleNamespace(intents=[], facts=[SimpleNamespace(id="fact-1")])
    monkeypatch.setattr(graph_store, "project_detail", lambda conn, pid: detail)
    monkeypatch.setattr(
        edge_store, "create_intent",
        lambda conn, project_id, sources, direction, owner, worker=None: (
            created.append((project_id, sources, direction, worker))
            or SimpleNamespace(id="intent-new")
        ),
    )
    monkeypatch.setattr(graph_store, "add_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(graph_store, "link_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(graph_store, "add_link", lambda *args, **kwargs: None)
    service, _log, events = _service()
    assert service._ensure_primary_intent(
        {"id": "run-1"}, {"id": "ch-1", "metadata": {"attempts": 3}},
        "proj-1", "amber",
    ) == "intent-new"
    assert created and "第 3 次接手" in created[0][2]
    assert created[0][3] == "amber"
    assert events[-1][1]["source"] == "created"
