from __future__ import annotations

import sqlite3

from backend.blackboard.ids import next_intent_id, utcnow
from backend.blackboard.models import Intent


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def list_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent(conn: sqlite3.Connection, project_id: str, intent_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?", (intent_id, project_id)
    ).fetchone()


def create_intent(
    conn: sqlite3.Connection,
    project_id: str,
    from_ids: list[str],
    description: str,
    creator: str,
    worker: str | None = None,
) -> Intent:
    now = utcnow()
    iid = next_intent_id(conn, project_id)
    claimed = worker is not None
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
        "last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
        (iid, project_id, description, creator, worker, now if claimed else None, now),
    )
    for fid in from_ids:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (iid, project_id, fid),
        )
    row = get_intent(conn, project_id, iid)
    return intent_to_model(conn, row, project_id)


def claim_intent(conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str) -> None:
    now = utcnow()
    conn.execute(
        "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
        (worker, now, intent_id, project_id),
    )


def release_intent(conn: sqlite3.Connection, project_id: str, intent_id: str) -> None:
    conn.execute(
        "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    )


def conclude_intent(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str, fact_id: str
) -> None:
    now = utcnow()
    conn.execute(
        "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? "
        "WHERE id = ? AND project_id = ?",
        (fact_id, worker, now, now, intent_id, project_id),
    )


def expire_workers(conn: sqlite3.Connection, timeout: int, project_id: str | None = None) -> None:
    now = utcnow()
    query = """
        UPDATE intents SET worker = NULL
        WHERE to_fact_id IS NULL AND worker IS NOT NULL AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
