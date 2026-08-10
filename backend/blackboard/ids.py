from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: Any) -> str:
    row = conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = 'project' RETURNING value"
    ).fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(conn: Any, kind: str, prefix: str, project_id: str) -> str:
    conn.execute(
        "INSERT INTO scoped_counters (project_id, kind, value) VALUES (%s, %s, 0) "
        "ON CONFLICT (project_id, kind) DO NOTHING",
        (project_id, kind),
    )
    row = conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = %s AND kind = %s "
        "RETURNING value",
        (project_id, kind),
    ).fetchone()
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: Any, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: Any, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: Any, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_report_id(conn: Any, project_id: str) -> str:
    return _next_scoped_id(conn, "report", "r", project_id)


def next_attachment_id(conn: Any, project_id: str) -> str:
    return _next_scoped_id(conn, "attachment", "a", project_id)
