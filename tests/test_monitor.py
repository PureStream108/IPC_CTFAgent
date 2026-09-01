from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.blackboard import graph_store, node_store
from backend.blackboard.db import Database
from backend.core.monitor import (
    STALL_HIGH_MINUTES,
    STALL_MEDIUM_MINUTES,
    MonitorVerdict,
    assess_project,
    escalated,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _detail(db, pid: str):
    with db.connect() as conn:
        return graph_store.project_detail(conn, pid)


def _project(db, category: str = "web") -> str:
    with db.connect() as conn:
        return graph_store.create_project(conn, "T", "origin", "get flag", category)


def _age_project(db, pid: str, minutes: float) -> None:
    old = (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as conn:
        conn.execute("UPDATE projects SET created_at = %s WHERE id = %s", (old, pid))


def test_assess_fresh_project_is_low():
    db = Database().configure()
    pid = _project(db)
    verdict = assess_project(_detail(db, pid), now=NOW)
    assert verdict.difficulty == "low"
    assert verdict.evidence == []


def test_assess_detects_multiple_exploit_classes():
    db = Database().configure()
    pid = _project(db)
    with db.connect() as conn:
        node_store.create_fact(conn, pid, "union select worked, sqli confirmed")
        node_store.create_fact(conn, pid, "jinja template injection via ssti param")
    verdict = assess_project(_detail(db, pid), now=NOW)
    assert verdict.difficulty == "medium"
    assert "distinct_exploit_classes:2" in verdict.evidence


def test_assess_escalates_on_stalled_project():
    db = Database().configure()
    pid = _project(db)
    _age_project(db, pid, STALL_MEDIUM_MINUTES + 1)
    verdict = assess_project(_detail(db, pid), now=NOW)
    assert verdict.difficulty == "medium"
    assert any(e.startswith("no_new_fact_minutes:") for e in verdict.evidence)

    _age_project(db, pid, STALL_HIGH_MINUTES + 1)
    verdict = assess_project(_detail(db, pid), now=NOW)
    assert verdict.difficulty == "high"


def test_assess_counts_struggle_signals():
    db = Database().configure()
    pid = _project(db)
    verdict = assess_project(_detail(db, pid), struggle_count=4, now=NOW)
    assert verdict.difficulty == "high"
    assert "struggle_count:4" in verdict.evidence


def test_assess_combines_to_ex():
    db = Database().configure()
    pid = _project(db)
    with db.connect() as conn:
        node_store.create_fact(conn, pid, "sqli via union select on login")
        node_store.create_fact(conn, pid, "ssti in jinja template")
        node_store.create_fact(conn, pid, "ssrf to 169.254.169.254 metadata")
        node_store.create_fact(conn, pid, "deserialization pickle rce sink")
    verdict = assess_project(_detail(db, pid), now=NOW)
    assert verdict.difficulty == "ex"
    assert "distinct_exploit_classes:4+" in verdict.evidence


def test_escalated_only_on_strict_increase():
    assert escalated(None, "high") is False  # baseline observation never fires
    assert escalated("low", "medium") is True
    assert escalated("medium", "high") is True
    assert escalated("high", "high") is False
    assert escalated("high", "low") is False


def test_verdict_defaults():
    verdict = MonitorVerdict()
    assert verdict.difficulty == "low"
    assert verdict.evidence == []
