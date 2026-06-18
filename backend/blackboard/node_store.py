from __future__ import annotations

import sqlite3

from backend.blackboard.ids import next_fact_id, utcnow
from backend.blackboard.models import Fact


def insert_fact(conn: sqlite3.Connection, project_id: str, fact_id: str, description: str) -> Fact:
    now = utcnow()
    conn.execute(
        "INSERT INTO facts (id, project_id, description, created_at) VALUES (?, ?, ?, ?)",
        (fact_id, project_id, description, now),
    )
    return Fact(id=fact_id, description=description, created_at=now)


def create_fact(conn: sqlite3.Connection, project_id: str, description: str) -> Fact:
    fid = next_fact_id(conn, project_id)
    return insert_fact(conn, project_id, fid, description)


def list_facts(conn: sqlite3.Connection, project_id: str) -> list[Fact]:
    rows = conn.execute(
        "SELECT id, description, created_at FROM facts WHERE project_id = ? ORDER BY rowid",
        (project_id,),
    ).fetchall()
    return [Fact(id=r["id"], description=r["description"], created_at=r["created_at"]) for r in rows]


def fact_exists(conn: sqlite3.Connection, project_id: str, fact_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fact_id, project_id)
    ).fetchone()
    return row is not None
