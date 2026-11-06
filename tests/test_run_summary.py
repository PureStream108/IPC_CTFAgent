"""summary.json is the operator-facing scorecard the plan asks for."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.competition.service import CompetitionService

NOW = datetime(2026, 10, 25, 2, 26, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.statements.append(text)
        if "COUNT(DISTINCT challenge_id)" in text:
            return _Cursor({"attempted": 4})
        if "kind='session.resumed'" in text:
            return _Cursor({"resumed": 2})
        if "slot.turnover_ms" in text:
            return _Cursor(many=[{"ms": 100}, {"ms": 300}, {"ms": 900}])
        if "SUM(EXTRACT(EPOCH" in text:
            return _Cursor({"seconds": 5400})
        return _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Db:
    def __init__(self):
        self.connection = _Connection()

    def connect(self):
        return self.connection


def _service(tmp_path=None):
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(
        db=_Db(),
        artifact_root=tmp_path,
        config=SimpleNamespace(
            member=SimpleNamespace(model="deepseek-v4.1-flash", api_surface="auto"),
            member_backup=None,
        ),
    )
    service.store = SimpleNamespace(
        challenges=lambda run_id: [
            {"id": "a", "state": "solved", "metadata": {"attempts": 0}},
            {"id": "b", "state": "ready", "metadata": {"attempts": 2}},
            {"id": "c", "state": "solving", "metadata": {"attempts": 0}},
            {"id": "d", "state": "ready", "metadata": {}},
        ],
        append_run_event=lambda *args, **kwargs: None,
    )
    service._remote_instance_limit = lambda run, platform: 3
    service._platform_for_run = lambda run: None
    return service


def _run():
    return {
        "id": "run-1",
        "status": "finished",
        "created_at": NOW.isoformat(),
        "config_snapshot": {"workflow_spec": {
            "ends_at": (NOW + timedelta(hours=6)).isoformat(),
            "duration_minutes": 0,
        }},
    }


def test_summary_carries_window_slots_challenges_and_model():
    service = _service()
    summary = service._run_summary(_run())

    assert summary["window"]["source"] == "competition"
    assert summary["window"]["minutes"] == 360.0
    assert summary["window"]["local_guard_minutes"] == 0
    assert summary["slots"]["limit"] == 3
    assert 0 <= summary["slots"]["utilization"] <= 1
    assert summary["slots"]["turnover_p95_ms"] == 900
    assert summary["challenges"] == {
        "total": 4, "attempted": 4, "solved": 1, "running": 1,
        "parked": 1, "resumed": 2,
    }
    assert summary["model"]["id"] == "deepseek-v4.1-flash"
    assert summary["model"]["key_slot"] == "primary"


def test_an_unbounded_run_has_no_window_and_no_utilization_ceiling():
    service = _service()
    run = _run()
    run["config_snapshot"] = {"workflow_spec": {}}
    summary = service._run_summary(run)
    assert summary["window"]["source"] == "unbounded"
    assert summary["window"]["minutes"] is None


def test_write_run_summary_persists_json(tmp_path):
    service = _service(tmp_path)
    service._run_summary = lambda run: {"run_id": run["id"], "ok": True}
    service._write_run_summary({"id": "run-1"})
    path = tmp_path / "exports" / "competition" / "run-1-summary.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"run_id": "run-1", "ok": True}
