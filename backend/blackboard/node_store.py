from __future__ import annotations

import re
import sqlite3

from backend.blackboard.ids import next_fact_id, utcnow
from backend.blackboard.models import Fact


# Agent reports often restate the same source finding with a different lead-in
# (for example, "Nday candidate" versus "confirmed candidate").  Keeping the
# full prose is useful for audit, but these stable tokens are enough to prevent
# a repeated conclusion from inflating the graph and retriggering Diamond.
_FINDING_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.(?:c|cc|cpp|cxx|h|hpp|py|js|ts|java))"
    r"(?::|\s+lines?\s+)(?P<line>\d+(?:-\d+)?)"
    r".*?(?P<commit>\b[0-9a-f]{7,40}\b)",
    re.IGNORECASE | re.DOTALL,
)


def _finding_key(description: str) -> tuple[str, str, str] | None:
    match = _FINDING_RE.search(description)
    if match is None:
        return None
    return (
        match.group("path").replace("\\", "/").lower(),
        match.group("line"),
        match.group("commit").lower(),
    )


def _existing_equivalent_fact(
    conn: sqlite3.Connection, project_id: str, description: str
) -> Fact | None:
    normalized = " ".join(description.split())
    row = conn.execute(
        "SELECT id, description, created_at FROM facts "
        "WHERE project_id = ? AND description = ? ORDER BY rowid LIMIT 1",
        (project_id, normalized),
    ).fetchone()
    if row is not None:
        return Fact(id=row["id"], description=row["description"], created_at=row["created_at"])

    key = _finding_key(normalized)
    if key is None:
        return None
    for row in conn.execute(
        "SELECT id, description, created_at FROM facts WHERE project_id = ? ORDER BY rowid",
        (project_id,),
    ).fetchall():
        if _finding_key(row["description"]) == key:
            return Fact(id=row["id"], description=row["description"], created_at=row["created_at"])
    return None


def insert_fact(conn: sqlite3.Connection, project_id: str, fact_id: str, description: str) -> Fact:
    now = utcnow()
    conn.execute(
        "INSERT INTO facts (id, project_id, description, created_at) VALUES (?, ?, ?, ?)",
        (fact_id, project_id, description, now),
    )
    return Fact(id=fact_id, description=description, created_at=now)


def create_fact(conn: sqlite3.Connection, project_id: str, description: str) -> Fact:
    description = " ".join(description.split())
    existing = _existing_equivalent_fact(conn, project_id, description)
    if existing is not None:
        return existing
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
