from __future__ import annotations

import hashlib
from pathlib import Path

from backend.blackboard import graph_store
from backend.blackboard.ids import utcnow
from backend.core.wp_writer import validate_writeup


class FlagConflictError(ValueError):
    """A project already committed a different verified flag."""


def assert_flag_compatible(connection, project_id: str, flag: str) -> dict:
    """Check a candidate Flag while holding the project completion mutex.

    Finalizers can call this before writing filesystem artifacts.  Keeping the
    project row lock open until the eventual commit prevents a concurrent
    finalizer from changing the accepted Flag between the check and write.
    """

    normalized = flag.strip()
    if not normalized:
        raise ValueError("flag must not be empty")
    project = connection.execute(
        "SELECT flag, status, flag_verified_at FROM projects WHERE id = %s FOR UPDATE",
        (project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("project not found")
    existing_flag = str(project["flag"] or "").strip()
    if existing_flag and existing_flag != normalized:
        raise FlagConflictError("project already has a different flag")
    verified = connection.execute(
        """
        SELECT normalized_flag FROM flag_submissions
        WHERE project_id = %s AND status = 'verified'
        ORDER BY verified_at, id
        """,
        (project_id,),
    ).fetchall()
    if any(str(row["normalized_flag"] or "").strip() != normalized for row in verified):
        raise FlagConflictError("project already has a different verified flag submission")
    return project


def verify_flag(db, project_id: str) -> dict:
    """Verify the durable solve evidence without depending on post-processing."""

    reasons: list[str] = []
    with db.connect() as connection:
        row = graph_store.get_project_row(connection, project_id)
        if row is None:
            return {"ok": False, "reasons": ["project not found"]}
        flag = str(row["flag"] or "").strip()
        has_goal_edge = connection.execute(
            "SELECT 1 FROM intents WHERE project_id = %s AND to_fact_id = 'goal'",
            (project_id,),
        ).fetchone() is not None

    if not flag:
        reasons.append("no flag recorded")
    if not has_goal_edge:
        reasons.append("no completion (goal) edge in graph")
    return {"ok": not reasons, "flag": flag or None, "reasons": reasons}


def accept_verified_flag(
    connection,
    project_id: str,
    flag: str,
    *,
    source: str,
    evidence_artifact: str | None = None,
) -> dict:
    """Commit a verified Flag and the solved outcome in one transaction."""

    normalized = flag.strip()
    project = assert_flag_compatible(connection, project_id, normalized)
    existing_flag = str(project["flag"] or "").strip()
    if project["status"] == "solved" and not existing_flag:
        raise ValueError("project is solved without a verified flag")
    has_goal_edge = connection.execute(
        "SELECT 1 FROM intents WHERE project_id = %s AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchone()
    if has_goal_edge is None:
        raise ValueError("cannot verify flag without a completion edge")
    key = hashlib.sha256(f"{project_id}\0{normalized}".encode("utf-8")).hexdigest()
    now = utcnow()
    connection.execute(
        """
        INSERT INTO flag_submissions (
            project_id, candidate, normalized_flag, status, source,
            evidence_artifact, idempotency_key, created_at, verified_at
        ) VALUES (%s, %s, %s, 'verified', %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO UPDATE
        SET status = 'verified',
            verified_at = COALESCE(flag_submissions.verified_at, EXCLUDED.verified_at),
            evidence_artifact = COALESCE(EXCLUDED.evidence_artifact, flag_submissions.evidence_artifact)
        """,
        (project_id, flag, normalized, source, evidence_artifact, key, now, now),
    )
    connection.execute(
        """
        UPDATE projects
        SET flag = %s, status = 'solved', flag_verified_at = COALESCE(flag_verified_at, %s),
            postprocess_status = CASE
                WHEN postprocess_status = 'completed' THEN postprocess_status ELSE 'pending' END,
            terminal_reason = NULL, updated_at = %s
        WHERE id = %s AND status <> 'solved'
        """,
        (normalized, now, now, project_id),
    )
    return {
        "ok": True,
        "flag": normalized,
        "verified_at": project["flag_verified_at"] or now,
        "idempotency_key": key,
    }


def verify_postprocess(db, project_id: str, wp_dir: Path) -> dict:
    reasons: list[str] = []
    with db.connect() as connection:
        row = graph_store.get_project_row(connection, project_id)
        if row is None:
            return {"ok": False, "reasons": ["project not found"]}
        flag = row["flag"]
        wp_path = row["wp_path"]

    if not wp_path or not Path(wp_path).is_file():
        reasons.append("writeup file missing")
    elif errors := validate_writeup(
        Path(wp_path).read_text(encoding="utf-8"),
        expected_flag=flag,
        require_complete=True,
    ):
        reasons.extend(f"invalid writeup: {error}" for error in errors)
    return {"ok": not reasons, "flag": flag, "wp_path": wp_path, "reasons": reasons}


def verify_flag_and_wp(db, project_id: str, wp_dir: Path) -> dict:
    """Compatibility aggregate used by older callers and export checks."""

    flag = verify_flag(db, project_id)
    postprocess = verify_postprocess(db, project_id, wp_dir)
    reasons = [*flag.get("reasons", []), *postprocess.get("reasons", [])]
    return {
        "ok": not reasons,
        "flag": flag.get("flag"),
        "wp_path": postprocess.get("wp_path"),
        "reasons": reasons,
    }
