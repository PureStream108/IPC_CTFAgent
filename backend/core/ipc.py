from __future__ import annotations

import hashlib
from pathlib import Path

from backend.blackboard import graph_store
from backend.blackboard.ids import utcnow
from backend.core.wp_writer import validate_writeup


class FlagConflictError(ValueError):
    """A project already committed a different verified flag."""


def lock_project_completion(connection, project_id: str) -> None:
    """Serialize per project every transaction spanning projects + postprocess_jobs.

    Finalizers lock the projects row and then insert postprocess jobs, while
    postprocess workers claim a job row and then update the projects row —
    opposite orders that deadlock under concurrency.  Taking one advisory
    transaction lock per project at both entry points breaks the cycle.
    """

    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (project_id,))


def assert_flag_compatible(connection, project_id: str, flag: str) -> dict:
    """Check a candidate Flag while holding the project completion mutex.

    Finalizers can call this before writing filesystem artifacts.  Keeping the
    project row lock open until the eventual commit prevents a concurrent
    finalizer from changing the accepted Flag between the check and write.
    """

    lock_project_completion(connection, project_id)
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
    supersede_pending_flags(connection, project_id)
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


# Flag submission lifecycle for platform-judged projects:
# pending -> judging -> verified | rejected | error (transient, may re-pend).
# ``superseded`` marks candidates that lost the race against an accepted flag.
PENDING_FLAG_STATUSES = ("pending", "judging", "error")


def record_pending_flag(
    connection,
    project_id: str,
    flag: str,
    *,
    source: str,
    evidence_artifact: str | None = None,
) -> dict:
    """Queue a candidate Flag for platform verdict without solving the project.

    The project row lock is held so a concurrent finalizer cannot accept a
    different Flag between our compatibility check and the insert.  Replays of
    the same candidate reuse the idempotency key: an already verified row
    stays verified, anything else is queued for (another) judgement.
    """

    normalized = flag.strip()
    project = assert_flag_compatible(connection, project_id, normalized)
    key = hashlib.sha256(f"{project_id}\0{normalized}".encode("utf-8")).hexdigest()
    now = utcnow()
    row = connection.execute(
        """
        INSERT INTO flag_submissions (
            project_id, candidate, normalized_flag, status, source,
            evidence_artifact, idempotency_key, created_at
        ) VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO UPDATE
        SET status = CASE
                WHEN flag_submissions.status = 'verified' THEN 'verified'
                ELSE 'pending' END,
            error = NULL,
            evidence_artifact = COALESCE(EXCLUDED.evidence_artifact, flag_submissions.evidence_artifact)
        RETURNING status
        """,
        (project_id, flag, normalized, source, evidence_artifact, key, now),
    ).fetchone()
    return {
        "ok": True,
        "mode": "verified" if row and row["status"] == "verified" else "pending",
        "flag": normalized,
        "idempotency_key": key,
        "project_status": project["status"],
    }


def requires_platform_verdict(project_row) -> bool:
    """A project solved on ret2shell must be judged by the platform, not locally."""

    return bool(
        project_row
        and project_row["platform"] == "ret2shell"
        and str(project_row["external_id"] or "").strip()
    )


def submit_flag_candidate(
    connection,
    project_id: str,
    flag: str,
    *,
    source: str,
    evidence_artifact: str | None = None,
) -> dict:
    """Record a candidate Flag: platform-judged when linked, local otherwise.

    Platform-linked projects only queue a pending submission here; the
    orchestrator's verdict worker promotes it via :func:`accept_verified_flag`
    once the platform judge confirms it.  All other projects keep the
    historical behaviour and are accepted immediately on local evidence.
    """

    project = connection.execute(
        "SELECT platform, external_id FROM projects WHERE id = %s",
        (project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("project not found")
    if requires_platform_verdict(project):
        return record_pending_flag(
            connection,
            project_id,
            flag,
            source=source,
            evidence_artifact=evidence_artifact,
        )
    result = accept_verified_flag(
        connection,
        project_id,
        flag,
        source=source,
        evidence_artifact=evidence_artifact,
    )
    result["mode"] = "verified"
    return result


def supersede_pending_flags(connection, project_id: str) -> None:
    """Close out queued candidates once one Flag has been accepted."""

    connection.execute(
        """
        UPDATE flag_submissions SET status = 'superseded'
        WHERE project_id = %s AND status = ANY(%s)
        """,
        (project_id, list(PENDING_FLAG_STATUSES)),
    )


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
