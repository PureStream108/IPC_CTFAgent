from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import monitor_real_competition as monitor

BASE = datetime(2026, 10, 14, tzinfo=timezone.utc)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def snapshot():
    return {
        "challenges": [{"id": "c1", "external_id": "1", "project_id": "p1", "state": "solved",
                        "metadata": {"category": "web", "ready_at": at(0).isoformat()}, "first_assigned_at": at(2)}],
        "assignment_rows": [{"session_id": "s1", "challenge_id": "c1", "role": "primary", "released_at": at(10)}],
        "submission_rows": [{"id": "f1", "challenge_id": "c1", "status": "correct", "created_at": at(4), "submitted_at": at(5), "candidate": "private-answer"}],
        "wp_jobs": [{"challenge_id": "c1", "status": "done", "attempts": 1}],
        "instance_rows": [], "messages": {"total": 0, "backlog": 0, "member_messages": 0},
        "agent_sessions": [], "events": [], "agent_events": [],
    }


def event(kind, seconds, **payload):
    return {"kind": kind, "created_at": at(seconds), "payload": payload}


def test_selection_is_exact_unique_unsolved_and_covers_categories():
    challenges = [{"external_id": str(i), "category": monitor.REQUIRED_CATEGORIES[i % 6], "solved": False}
                  for i in range(24)]
    challenges.append({**challenges[0]})
    challenges.append({"external_id": "solved", "category": "web", "solved": True})
    selected = monitor.choose_challenges(challenges)
    assert len(selected) == len(set(selected)) == 20
    assert "solved" not in selected
    with pytest.raises(RuntimeError, match="unsolved ai"):
        monitor.choose_challenges([row for row in challenges if row["category"] != "ai"])
    with pytest.raises(RuntimeError, match="unique unsolved"):
        monitor.choose_challenges(challenges[:7])


def test_redaction_handles_nested_native_messages_and_known_nonstandard_flags(tmp_path):
    report = {"run_id": "run", "status": "finished", "latency": {},
              "logs": [{"content": json.dumps({"flag": "nonstandard-answer", "api_key": "provider-key"})},
                       {"text": "URL https://user:secret@host/?token=abc and private-answer and flag{hidden}"}],
              "final_resources": {}, "anomalies": [], "recoveries": []}
    paths = monitor.export_report(report, tmp_path, ("provider-key", "private-answer"))
    for path in paths:
        content = path.read_text(encoding="utf-8")
        for secret in ("nonstandard-answer", "provider-key", "private-answer", "hidden", "token=abc", "user:secret"):
            assert secret not in content
    exported = json.loads(paths[0].read_text(encoding="utf-8"))
    assert exported["status"] == "finished"
    assert json.loads((tmp_path / "real_competition_run.json").read_text(encoding="utf-8")) == exported
    assert (tmp_path / "real_competition_run.md").is_file()
    with pytest.raises(ValueError, match="run id"):
        monitor.export_report({"run_id": "../escape"}, tmp_path)


def test_concurrent_timings_match_session_call_and_submission_ids():
    data = snapshot()
    data["submission_rows"].append({"id": "f2", "challenge_id": "c1", "status": "correct", "submitted_at": at(50)})
    run_events = [event("assignment.started", 2, challenge_id="c1", session_id="s1"),
                  event("member.started", 3, challenge_id="c1", session_id="s1"),
                  event("submission.verdict", 8, submission_id="f1"),
                  event("submission.verdict", 51, submission_id="f2")]
    agent_events = [
        {**event("tool_call", 4, id="same", name="mcp_tool"), "session_id": "s1"},
        {**event("tool_call", 100, id="same", name="mcp_tool"), "session_id": "s2"},
        {**event("tool_result", 6, id="same", message={"content": '{"status":"ok"}'}), "session_id": "s1"},
        {**event("tool_result", 107, id="same", message={"content": '{"error":"failed"}'}), "session_id": "s2"},
    ]
    measured = monitor.metrics(data, run_events, agent_events, [], [])
    assert measured["latency"]["flag_submit_to_verdict"] == {"count": 2, "p50": 2.0, "p95": 2.9, "max": 3.0}
    assert measured["latency"]["mcp_call_to_result"]["max"] == 7.0
    assert measured["latency"]["assignment_to_member_start"]["p50"] == 1.0
    assert measured["mcp"]["error_rate"] == 0.5
    assert measured["latency"]["provider_time_to_first_response"]["count"] == 0


def test_lowering_capacity_grandfathers_existing_instances_and_checks_new_admission():
    events = [event("instance.started", 1, challenge_id="a"), event("instance.started", 2, challenge_id="b"),
              event("run.limits_updated", 3, limits={"remote_concurrency": 1})]
    assert monitor.capacity_violations(events, 2) == []
    events.append(event("instance.started", 4, challenge_id="c"))
    assert monitor.capacity_violations(events, 2)[0]["previously_active"] == 2


def test_target_delivery_and_terminal_cleanup_are_checked_from_evidence():
    data = snapshot()
    data["challenges"][0]["metadata"]["remote"] = True
    data["agent_events"] = [{"session_id": "s1", "kind": "tool_call", "sequence": 1, "event_key": "call:1", "payload": {}}]
    measured = monitor.metrics(data, [], [], [], [])
    checks = monitor.acceptance_checks(data, measured, "finished", [])
    assert checks["target_before_first_tool"]["status"] == "fail"
    data["agent_events"].insert(0, {"session_id": "s1", "kind": "provider_messages", "sequence": 0,
                                  "event_key": "recon:1", "payload": {"kind": "target_instance"}})
    data["messages"]["backlog"] = 1
    measured = monitor.metrics(data, [], [], [], [])
    checks = monitor.acceptance_checks(data, measured, "finished", [])
    assert checks["target_before_first_tool"]["status"] == "pass"
    assert checks["collaboration_cleanup"]["status"] == "fail"


def test_monitor_uses_only_get_and_readonly_sql_and_saves_completed_evidence(tmp_path, monkeypatch):
    queries = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql): queries.append(sql)
    class Session:
        def get(self, url, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"run": {"status": "finished"}})
        def post(self, *_args, **_kwargs):
            pytest.fail("observation must not mutate the live application")
    monkeypatch.setattr(monitor.psycopg, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(monitor, "db_snapshot", lambda *args, **kwargs: copy.deepcopy(snapshot()))
    monkeypatch.setattr(monitor, "log_rows", lambda *args, **kwargs: [])
    report = monitor.monitor("http://local", "run", 1, "postgresql://unused", export=tmp_path, session=Session())
    assert report["termination_reason"] == "terminal_run"
    assert report["read_only_observation"] is True
    assert "SET default_transaction_read_only TO on" in queries
    assert report["checks"]["assignment_cleanup"]["status"] == "pass"
    assert (tmp_path / "real_competition_run_monitor.md").exists()


@pytest.mark.parametrize("resumed_successfully", [False, True])
def test_recovery_requires_a_successful_result_after_the_recovery_signal(tmp_path, monkeypatch, resumed_successfully):
    data = snapshot()
    data["events"] = [{**event("session.rebuilt", 5, session_id="s1"), "event_id": 1}]
    output = {"status": "ok"} if resumed_successfully else {"status": "unknown"}
    data["agent_events"] = [{**event("tool_result", 6, id="call", message={"content": json.dumps(output)}),
                             "event_id": 2, "session_id": "s1", "sequence": 1, "event_key": "tool-result:call"}]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql): return None
    class Session:
        def get(self, url, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"run": {"status": "finished"}})
    monkeypatch.setattr(monitor.psycopg, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(monitor, "db_snapshot", lambda *args, **kwargs: copy.deepcopy(data))
    monkeypatch.setattr(monitor, "log_rows", lambda *args, **kwargs: [])
    report = monitor.monitor("http://local", "run", 1, "postgresql://unused", export=tmp_path, session=Session())
    assert len(report["recoveries"]) == 1
    assert report["self_recovery_observed"] is resumed_successfully


@pytest.mark.postgres
def test_snapshot_reads_real_postgres_rows_under_readonly_transaction(tmp_path):
    import os

    import psycopg

    from backend.competition.fixture import FixturePlatform
    from backend.platform.mapping import PlatformChallenge
    from tests.test_competition_service_fixture import _new_fixture_service

    platform = FixturePlatform([PlatformChallenge(external_id="read", title="Read", category="misc", description="monitor")])
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "monitor")
    try:
        run_id = service.start(workflow_id, "monitor")['run']['id']
        service.tick()
        with psycopg.connect(os.environ["IPC_DATABASE_URL"], row_factory=psycopg.rows.dict_row, autocommit=True) as connection:
            connection.execute("SET default_transaction_read_only TO on")
            data = monitor.db_snapshot(connection, run_id)
        assert data["challenges"][0]["external_id"] == "read"
        assert data["assignment_rows"]
        assert data["events"]
    finally:
        service.shutdown()
        db.close()
