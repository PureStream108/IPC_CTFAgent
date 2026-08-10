from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any


POSTPROCESS_KINDS = ("writeup", "memory", "archive")


@dataclass(slots=True)
class PostprocessJob:
    id: int
    project_id: str
    kind: str
    attempts: int
    lease_token: str


def enqueue_postprocess(connection: Any, project_id: str) -> None:
    for kind in POSTPROCESS_KINDS:
        connection.execute(
            """
            INSERT INTO postprocess_jobs (project_id, kind, idempotency_key)
            VALUES (%s, %s, %s)
            ON CONFLICT (project_id, kind) DO NOTHING
            """,
            (project_id, kind, f"{project_id}:{kind}"),
        )
    refresh_project_status(connection, project_id)
    connection.execute("SELECT pg_notify('ipc_scheduler', %s)", (f"postprocess:{project_id}",))


def claim_next_job(
    connection: Any,
    worker_id: str,
    *,
    lease_seconds: int = 120,
) -> PostprocessJob | None:
    row = connection.execute(
        """
        SELECT id
        FROM postprocess_jobs
        WHERE status IN ('pending', 'retry')
          AND next_attempt_at <= now()
          AND (lease_expires_at IS NULL OR lease_expires_at < now())
          AND (
              kind <> 'archive'
              OR EXISTS (
                  SELECT 1 FROM postprocess_jobs writeup
                  WHERE writeup.project_id = postprocess_jobs.project_id
                    AND writeup.kind = 'writeup' AND writeup.status = 'completed'
              )
          )
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    token = secrets.token_urlsafe(24)
    claimed = connection.execute(
        """
        UPDATE postprocess_jobs
        SET status = 'running', attempts = attempts + 1, locked_by = %s,
            lease_token = %s,
            lease_expires_at = now() + make_interval(secs => %s),
            updated_at = now()
        WHERE id = %s
        RETURNING id, project_id, kind, attempts, lease_token
        """,
        (worker_id, token, lease_seconds, row["id"]),
    ).fetchone()
    refresh_project_status(connection, claimed["project_id"])
    return PostprocessJob(**claimed)


def renew_job(
    connection: Any,
    job: PostprocessJob,
    *,
    lease_seconds: int = 120,
) -> bool:
    """Extend an active job lease without allowing an expired token to revive."""

    return connection.execute(
        """
        UPDATE postprocess_jobs
        SET lease_expires_at = now() + make_interval(secs => %s),
            updated_at = now()
        WHERE id = %s AND lease_token = %s AND status = 'running'
          AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
        """,
        (lease_seconds, job.id, job.lease_token),
    ).rowcount == 1


def lock_job(
    connection: Any,
    job: PostprocessJob,
    *,
    lease_seconds: int = 120,
) -> bool:
    """Fence a job's commit phase and keep its lease live for short side effects.

    Callers hold the returned row lock until their transaction ends. Recovery
    and replacement workers therefore cannot enter the same filesystem or
    database commit phase concurrently.
    """

    row = connection.execute(
        """
        SELECT id
        FROM postprocess_jobs
        WHERE id = %s AND lease_token = %s AND status = 'running'
          AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
        FOR UPDATE
        """,
        (job.id, job.lease_token),
    ).fetchone()
    if row is None:
        return False
    connection.execute(
        """
        UPDATE postprocess_jobs
        SET lease_expires_at = now() + make_interval(secs => %s),
            updated_at = now()
        WHERE id = %s
        """,
        (lease_seconds, job.id),
    )
    return True


def complete_job(
    connection: Any,
    job: PostprocessJob,
    *,
    lease_locked: bool = False,
) -> bool:
    live_clause = "" if lease_locked else (
        " AND lease_expires_at IS NOT NULL AND lease_expires_at > now()"
    )
    changed = connection.execute(
        f"""
        UPDATE postprocess_jobs
        SET status = 'completed', locked_by = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error = NULL, updated_at = now()
        WHERE id = %s AND lease_token = %s AND status = 'running'
          {live_clause}
        """,
        (job.id, job.lease_token),
    ).rowcount == 1
    if changed:
        refresh_project_status(connection, job.project_id)
    return changed


def complete_existing_job(
    connection: Any,
    project_id: str,
    kind: str,
    *,
    lease_token: str | None = None,
) -> bool:
    """Mark an externally completed job without breaking worker fencing.

    An operator may have written a writeup before the asynchronous worker gets
    to it.  In that case only pending/retry jobs can be completed without a
    token.  A running job requires its exact lease token; a stale caller cannot
    overwrite another worker's result.
    """

    if lease_token is None:
        changed = connection.execute(
            """
            UPDATE postprocess_jobs
            SET status = 'completed', locked_by = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error = NULL, updated_at = now()
            WHERE project_id = %s AND kind = %s
              AND status IN ('pending', 'retry')
              AND locked_by IS NULL AND lease_token IS NULL
            """,
            (project_id, kind),
        ).rowcount == 1
    else:
        changed = connection.execute(
            """
            UPDATE postprocess_jobs
            SET status = 'completed', locked_by = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error = NULL, updated_at = now()
            WHERE project_id = %s AND kind = %s
              AND status = 'running' AND lease_token = %s
              AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
            """,
            (project_id, kind, lease_token),
        ).rowcount == 1

    # Completion is idempotent for a job already finalized by either path.  We
    # still refresh the aggregate status so callers see a consistent project.
    existing = connection.execute(
        "SELECT status FROM postprocess_jobs WHERE project_id = %s AND kind = %s",
        (project_id, kind),
    ).fetchone()
    if existing is not None and existing["status"] == "completed":
        changed = True
    if changed or existing is not None:
        refresh_project_status(connection, project_id)
    return changed


def fail_job(
    connection: Any,
    job: PostprocessJob,
    error: str,
    *,
    max_attempts: int = 4,
) -> bool:
    delay = min(900, 15 * (2 ** max(0, job.attempts - 1)))
    changed = connection.execute(
        """
        UPDATE postprocess_jobs
        SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'retry' END,
            next_attempt_at = now() + make_interval(secs => %s),
            last_error = %s, locked_by = NULL, lease_token = NULL,
            lease_expires_at = NULL, updated_at = now()
        WHERE id = %s AND lease_token = %s AND status = 'running'
          AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
        """,
        (max_attempts, delay, error[:4000], job.id, job.lease_token),
    ).rowcount == 1
    if changed:
        refresh_project_status(connection, job.project_id)
    return changed


def recover_expired_jobs(connection: Any) -> int:
    cursor = connection.execute(
        """
        UPDATE postprocess_jobs
        SET status = 'retry', locked_by = NULL, lease_token = NULL,
            lease_expires_at = NULL, next_attempt_at = now(),
            last_error = COALESCE(last_error, 'worker lease expired'), updated_at = now()
        WHERE status = 'running' AND lease_expires_at < now()
        """
    )
    return cursor.rowcount


def refresh_project_status(connection: Any, project_id: str) -> str:
    rows = connection.execute(
        "SELECT kind, status FROM postprocess_jobs WHERE project_id = %s",
        (project_id,),
    ).fetchall()
    statuses = {row["kind"]: row["status"] for row in rows}
    if not statuses:
        aggregate = "not_started"
    elif any(status == "failed" for status in statuses.values()):
        aggregate = "degraded"
    elif all(status == "completed" for status in statuses.values()):
        aggregate = "completed"
    elif any(status == "running" for status in statuses.values()):
        aggregate = "running"
    else:
        aggregate = "pending"
    connection.execute(
        "UPDATE projects SET postprocess_status = %s, updated_at = now() WHERE id = %s",
        (aggregate, project_id),
    )
    return aggregate
