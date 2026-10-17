from __future__ import annotations

import logging
from typing import Any

from backend.agent.jobs import INLINE_LIMIT, AgentJobStore, JobConflict, new_job_id
from backend.sandbox.command_policy import host_path_violation

_recovery_logger = logging.getLogger(__name__)

class JobService:
    """Bind a job store to one session's sandbox."""

    def __init__(self, store: AgentJobStore, sandbox, session_id: str) -> None:
        self.store = store
        self.sandbox = sandbox
        self.session_id = session_id


    def start(self, command: str, *, kind: str = "bash") -> dict[str, Any]:
        """Start a detached command after the same policy check as ``bash``.

        The policy check is not optional here.  Without it a long job would be
        a way to run exactly the commands ``bash`` refuses, which would make
        the detached path a hole straight through the command policy.
        """
        command = (command or "").strip()
        if not command:
            return {"ok": False, "error": "a non-empty command is required"}
        violation = host_path_violation(command)
        if violation is not None:
            return {"ok": False, "error": f"command blocked: {violation}"}

        job_id = new_job_id()
        self.store.start(
            self.session_id, command, kind=kind, job_id=job_id,
            workdir=getattr(self.sandbox, "workdir", None),
        )
        try:
            pid = self.sandbox.exec_detached(job_id, command)
        except Exception as exc:
            _recovery_logger.debug('Error handled in start', exc_info=True)
            self.store.finish(job_id, "lost")
            return {
                "ok": False,
                "job_id": job_id,
                "error": f"failed to start job: {type(exc).__name__}: {exc}",
            }
        self.store.record_pid(job_id, pid)
        return {
            "ok": True,
            "job_id": job_id,
            "state": "running",
            "pid": pid,
            "note": (
                "Job runs without a timeout. Poll job_status and read output "
                "with job_logs; cancel it with job_cancel when the evidence is "
                "enough."
            ),
        }


    def status(self, job_id: str) -> dict[str, Any]:
        try:
            row = self.store.job(job_id)
        except KeyError:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        if row["session_id"] != self.session_id:
            return {"ok": False, "error": "job belongs to another session"}
        if row["state"] != "running":
            return self._view(row)
        return self._view(self._reconcile(row))

    def wait(self, job_id: str, *, timeout: int = 5, cancel=None) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + max(1, min(15, timeout))
        while True:
            result = self.status(job_id)
            if result.get("state") != "running" or not result.get("ok", True):
                return result
            if cancel is not None and cancel.is_set():
                return {**result, "wait_cancelled": True}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**result, "wait_timed_out": True}
            if cancel is not None:
                cancel.wait(min(.5, remaining))
            else:
                time.sleep(min(.5, remaining))

    def _reconcile(self, row: dict) -> dict:
        """Fold the sandbox's observation of a running job into its row."""
        try:
            observed = self.sandbox.job_state(row["id"])
        except Exception:
            _recovery_logger.debug('Error handled in _reconcile', exc_info=True)
            return row
        state = observed.get("state")
        if state == "running":
            return self.store.note_progress(row["id"])
        if state == "exited":
            return self.store.finish(
                row["id"], "exited", exit_code=observed.get("exit_code")
            )
        return self.store.finish(row["id"], "lost")

    def jobs(self, *, active_only: bool = False) -> dict[str, Any]:
        rows = self.store.jobs(self.session_id, active_only=active_only)
        return {
            "ok": True,
            "jobs": [
                self._view(self._reconcile(row) if row["state"] == "running" else row)
                for row in rows
            ],
        }


    def logs(
        self, job_id: str, *, stream: str = "stdout", offset: int | None = None,
        limit: int = INLINE_LIMIT,
    ) -> dict[str, Any]:
        """Read a slice of a job's output by byte offset.

        Omitting ``offset`` continues from the stored cursor, so a model can
        poll repeatedly without re-reading what it has already seen.  Byte
        offsets rather than lines: a half-written final line would otherwise
        shift the cursor and duplicate output on the next poll.
        """
        try:
            row = self.store.job(job_id)
        except KeyError:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        if row["session_id"] != self.session_id:
            return {"ok": False, "error": "job belongs to another session"}
        if stream not in {"stdout", "stderr"}:
            return {"ok": False, "error": "stream must be stdout or stderr"}
        column = f"{stream}_offset"
        start = int(row[column]) if offset is None else max(0, int(offset))
        capped = max(1, min(int(limit or INLINE_LIMIT), INLINE_LIMIT))
        try:
            text, next_offset = self.sandbox.job_output(
                job_id, stream, offset=start, limit=capped
            )
        except Exception as exc:
            _recovery_logger.debug('Error handled in logs', exc_info=True)
            return {
                "ok": False,
                "job_id": job_id,
                "error": f"failed to read job output: {type(exc).__name__}: {exc}",
            }
        if offset is None and next_offset > start:
            self.store.note_progress(
                job_id, **{column: next_offset}
            )
        return {
            "ok": True,
            "job_id": job_id,
            "stream": stream,
            "offset": start,
            "next_offset": next_offset,
            "text": text,
            "truncated": next_offset - start >= capped,
        }


    def cancel(self, job_id: str) -> dict[str, Any]:
        try:
            row = self.store.job(job_id)
        except KeyError:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        if row["session_id"] != self.session_id:
            return {"ok": False, "error": "job belongs to another session"}
        if row["state"] != "running":
            return {"ok": True, "job_id": job_id, "state": row["state"],
                    "note": "job had already finished"}
        try:
            self.sandbox.job_cancel(job_id)
        except Exception as exc:
            _recovery_logger.debug('Error handled in cancel', exc_info=True)
            return {
                "ok": False,
                "job_id": job_id,
                "error": f"failed to cancel job: {type(exc).__name__}: {exc}",
            }
        final = self.store.finish(job_id, "cancelled")
        return {"ok": True, "job_id": job_id, "state": final["state"]}


    @staticmethod
    def _view(row: dict) -> dict[str, Any]:
        view = {
            "job_id": row["id"],
            "state": row["state"],
            "command": row["command"],
            "exit_code": row["exit_code"],
            "stdout_offset": row["stdout_offset"],
            "stderr_offset": row["stderr_offset"],
        }
        if row["state"] == "lost":
            view["warning"] = (
                "The process is gone and no exit status was recorded, so the "
                "result is unknown. Do not treat this as success; re-run the "
                "work or verify it another way."
            )
        return view


__all__ = ["AgentJobStore", "JobConflict", "JobService"]
