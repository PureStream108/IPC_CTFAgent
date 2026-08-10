from __future__ import annotations

from typing import Any

from backend.blackboard.ids import next_fact_id, utcnow
from backend.blackboard.models import Fact


def insert_fact(conn: Any, project_id: str, fact_id: str, description: str) -> Fact:
    now = utcnow()
    conn.execute(
        "INSERT INTO facts (id, project_id, description, created_at) VALUES (%s, %s, %s, %s)",
        (fact_id, project_id, description, now),
    )
    return Fact(id=fact_id, description=description, created_at=now)


def reserve_fact(conn: Any, project_id: str, description: str) -> Fact:
    """Allocate a fact id before a caller performs its fenced edge update."""

    return Fact(
        id=next_fact_id(conn, project_id),
        description=description,
        created_at=utcnow(),
    )


def create_fact(conn: Any, project_id: str, description: str) -> Fact:
    fid = next_fact_id(conn, project_id)
    return insert_fact(conn, project_id, fid, description)


def list_facts(conn: Any, project_id: str) -> list[Fact]:
    rows = conn.execute(
        "SELECT id, description, created_at FROM facts WHERE project_id = %s ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    return [Fact(id=r["id"], description=r["description"], created_at=r["created_at"]) for r in rows]


def fact_exists(conn: Any, project_id: str, fact_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM facts WHERE id = %s AND project_id = %s", (fact_id, project_id)
    ).fetchone()
    return row is not None
