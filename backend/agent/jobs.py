"""Durable records for sandbox work that outlives the turn that started it.

Every Member command used to run under ``timeout -k 5s 60s``, so a brute force
pass, a large file analysis or a slow scan was killed rather than finished.
A job is started detached and polled instead; this module is the durable half
of that, and the reason a poll still works after the worker restarts.

Two rules shape it:

* A job's handle is its row plus the files in the workspace volume, never a
  docker exec id.  Exec ids die with the container; the volume does not.
* ``lost`` is a real state.  A job whose process is gone with no recorded exit
  status is reported as ``lost``, never as ``exited 0`` -- "unknown" and
  "succeeded" are completely different evidence to a model deciding what to do
  next.
"""
from __future__ import annotations

import uuid


class JobConflict(ValueError):
    """The requested job transition contradicts a durable invariant."""


JOB_STATES = ("running", "exited", "cancelled", "lost")
INLINE_LIMIT = 32_768


def new_job_id() -> str:
    return uuid.uuid4().hex


class AgentJobStore:
    """Every method is one short transaction; none waits on a job."""

    def __init__(self, db):
        self.db = db

    def start(
        self,
        session_id: str,
        command: str,
        *,
        kind: str = "bash",
        job_id: str | None = None,
        workdir: str | None = None,
        pid: int | None = None,
    ) -> dict:
        if not command.strip():
            raise JobConflict("a job command is required")
        identifier = job_id or new_job_id()
        with self.db.connect() as connection:
            return connection.execute(
                """INSERT INTO agent_jobs
                   (id,session_id,kind,command,workdir,pid)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                (identifier, session_id, kind, command, workdir, pid),
            ).fetchone()

    def job(self, job_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_jobs WHERE id=%s", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return row

    def jobs(
        self, session_id: str, *, active_only: bool = False, limit: int = 50
    ) -> list[dict]:
        clause = " AND state='running'" if active_only else ""
        with self.db.connect() as connection:
            return connection.execute(
                f"""SELECT * FROM agent_jobs WHERE session_id=%s{clause}
                    ORDER BY started_at DESC, id LIMIT %s""",
                (session_id, max(1, min(limit, 200))),
            ).fetchall()

    def record_pid(self, job_id: str, pid: int) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_jobs SET pid=%s,last_progress_at=now()
                   WHERE id=%s RETURNING *""",
                (pid, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return row

    def note_progress(
        self,
        job_id: str,
        *,
        stdout_offset: int | None = None,
        stderr_offset: int | None = None,
    ) -> dict:
        """Advance the read cursors and the liveness timestamp.

        Offsets only move forward (``GREATEST``): a re-read from an older
        offset is a legitimate retry and must not rewind the cursor for the
        next incremental poll.
        """
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_jobs
                   SET stdout_offset=GREATEST(stdout_offset,COALESCE(%s,stdout_offset)),
                       stderr_offset=GREATEST(stderr_offset,COALESCE(%s,stderr_offset)),
                       last_progress_at=now()
                   WHERE id=%s RETURNING *""",
                (stdout_offset, stderr_offset, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return row

    def finish(
        self,
        job_id: str,
        state: str,
        *,
        exit_code: int | None = None,
        stdout_artifact: str | None = None,
        stderr_artifact: str | None = None,
    ) -> dict:
        """Record a terminal state once; a later observation cannot overwrite it.

        The ``state='running'`` guard is what makes polling safe to repeat: the
        first terminal observation wins, so a cancel that races the process's
        own exit cannot turn a recorded exit code back into ``cancelled``.
        """
        if state not in JOB_STATES or state == "running":
            raise JobConflict(f"not a terminal job state: {state}")
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_jobs SET state=%s,exit_code=COALESCE(%s,exit_code),
                       stdout_artifact=COALESCE(%s,stdout_artifact),
                       stderr_artifact=COALESCE(%s,stderr_artifact),
                       last_progress_at=now(),finished_at=now()
                   WHERE id=%s AND state='running' RETURNING *""",
                (state, exit_code, stdout_artifact, stderr_artifact, job_id),
            ).fetchone()
            if row is not None:
                return row
            existing = connection.execute(
                "SELECT * FROM agent_jobs WHERE id=%s", (job_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(job_id)
            return existing

    def mark_lost_for_session(self, session_id: str) -> list[dict]:
        """Resolve jobs left ``running`` by a container that went away.

        Called when a session's sandbox is known to be gone.  Deliberately
        ``lost`` rather than ``exited``: the exit status was never recorded, so
        claiming an exit code here would invent evidence.
        """
        with self.db.connect() as connection:
            return connection.execute(
                """UPDATE agent_jobs SET state='lost',last_progress_at=now(),
                       finished_at=now()
                   WHERE session_id=%s AND state='running' RETURNING *""",
                (session_id,),
            ).fetchall()

    def sweep_stale(self, *, idle_seconds: int, limit: int = 200) -> list[dict]:
        """Mark long-silent ``running`` jobs ``lost``.

        A job whose process vanished without recording an exit status leaves a
        permanently ``running`` row (observed: a container removed out from
        under a background job).  No poller will ever close it, so a periodic
        sweep closes any job whose ``last_progress_at`` is older than
        ``idle_seconds``.  ``lost`` (never ``exited``) because the exit status
        was never seen -- inventing an exit code here would fabricate evidence.

        Returns the rows it closed so the caller can emit one event per job.
        """
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        with self.db.connect() as connection:
            return connection.execute(
                """UPDATE agent_jobs SET state='lost',finished_at=now()
                   WHERE id IN (
                     SELECT id FROM agent_jobs
                     WHERE state='running'
                       AND last_progress_at < now() - make_interval(secs => %s)
                     ORDER BY last_progress_at LIMIT %s
                   ) RETURNING *""",
                (idle_seconds, max(1, min(limit, 1000))),
            ).fetchall()
