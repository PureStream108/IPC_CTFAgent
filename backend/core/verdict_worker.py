from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Callable

from backend.blackboard import graph_store
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.platform.ret2shell import (
    Ret2ShellError,
    Ret2ShellPreflightError,
    Ret2ShellRateLimitError,
)

# Memory categories whose stale conclusions must not survive a platform
# rejection (a wrong "exploit: Flag: ..." memory would endorse the same wrong
# flag on the next run). The rejected-flag blacklist itself always survives.
_REOPEN_PURGE_CATEGORIES = ("exploit", "lessons")
_REOPEN_KEEP_TAGS = ("rejected-flag", "flag-format")


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class VerdictWorker:
    """Platform-verdict gate for project completion.

    A project with an external_id only reaches ``completed`` after the
    platform accepts its flag. A rejected flag feeds back into memory (the
    rejected-flag blacklist) and the hints channel, then the project is
    reopened for another attempt. Verdicts are persisted in the submissions
    ledger keyed by (project_id, flag), so a re-derived new flag is never
    blocked by an older rejected record.
    """

    def __init__(
        self,
        state,
        *,
        client_factory: Callable[[], Any | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self.lifecycle = Lifecycle(state.db)
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._retry_not_before: dict[tuple[str, str], float] = {}

    # ---- client ----

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        return self.state.platform_client()

    # ---- periodic entry point (orchestrator tick) ----

    def process_pending(self) -> None:
        with self.state.db.connect() as conn:
            rows = graph_store.pending_verdict_projects(conn)
        for row in rows:
            try:
                self.process_one(row["id"])
            except Exception as exc:
                try:
                    self.state.logger.project(
                        "verdict_error", row["id"], error=f"{type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass

    # ---- single project ----

    def process_one(self, project_id: str) -> dict[str, Any] | None:
        state = self.state
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row is None or row["status"] != "pending_verdict":
                return None
            flag = (row["flag"] or "").strip()
            external_id = (row["external_id"] or "").strip()
            if not flag or not external_id:
                return None
            graph_store.record_submission(conn, project_id, flag)
            sub = graph_store.get_submission(conn, project_id, flag)
        if sub["status"] != "pending":
            return None
        key = (project_id, flag)
        if self._monotonic() < self._retry_not_before.get(key, 0.0):
            return None
        return self.submit_and_apply(project_id, flag)

    def submit_and_apply(
        self, project_id: str, flag: str, *, retry_backoff: bool = True
    ) -> dict[str, Any] | None:
        """Submit one flag to the platform and apply the verdict.

        Returns the ledger state ``{"solved": bool | None, "verdict": str}``
        or ``None`` when the platform could not be asked right now (no client,
        quota exhausted) — the pending row is kept for a later retry.
        """
        state = self.state
        runtime = state.config.runtime
        key = (project_id, flag)
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row is None:
                return None
            title = row["title"]
            external_id = (row["external_id"] or "").strip()
        if not external_id:
            return None
        client = self._client()
        if client is None:
            state.logger.project("verdict_no_platform_client", project_id)
            return None
        try:
            challenge_id = int(external_id)
        except (TypeError, ValueError):
            self._record(project_id, flag, status="unknown", verdict=f"bad external_id {external_id!r}")
            return {"solved": None, "verdict": "bad external_id"}

        def backoff() -> None:
            if retry_backoff:
                self._retry_not_before[key] = (
                    self._monotonic() + runtime.verdict_retry_seconds
                )

        try:
            status = client.challenge_status(challenge_id)
            if isinstance(status, dict) and status.get("solved"):
                return self._apply(project_id, title, flag, True, "already solved on platform", None)
            result = client.submit_flag(challenge_id, flag, check_solved=False)
        except Ret2ShellRateLimitError as exc:
            # Quota exhausted (local limiter or HTTP 429): keep the row pending
            # and retry next cycle — the task is never dropped.
            state.logger.project("verdict_rate_limited", project_id, error=str(exc))
            backoff()
            return None
        except Ret2ShellPreflightError as exc:
            return self._apply(project_id, title, flag, False, str(exc), None)
        except Ret2ShellError as exc:
            self._bump_attempts(project_id, flag, runtime, verdict=str(exc))
            backoff()
            return None
        solved = result.get("solved")
        verdict = str(result.get("result") or "")
        if solved is None:
            # Async judge did not answer in time: retry, then give up and
            # reopen so the attempt is not silently lost.
            attempts = self._bump_attempts(project_id, flag, runtime, verdict=verdict or "judge timeout")
            backoff()
            if attempts >= runtime.verdict_max_attempts:
                self._record(project_id, flag, status="unknown", verdict=verdict or "judge timeout")
                self._reopen(project_id, title, flag, "platform judge did not return a verdict")
                return {"solved": None, "verdict": "unknown"}
            return {"solved": None, "verdict": "pending"}
        return self._apply(project_id, title, flag, bool(solved), verdict, result.get("id"))

    # ---- verdict application ----

    def _apply(
        self,
        project_id: str,
        title: str,
        flag: str,
        solved: bool,
        verdict: str,
        submission_id: Any,
    ) -> dict[str, Any]:
        state = self.state
        self._record(
            project_id,
            flag,
            status="solved" if solved else "rejected",
            verdict=verdict,
            submission_id=None if submission_id is None else str(submission_id),
        )
        if solved:
            self._complete(project_id, flag)
            state.logger.project("platform_verdict", project_id, solved=True, flag=flag)
        else:
            self._feedback(project_id, title, flag, verdict)
            self._reopen(project_id, title, flag, verdict)
            state.logger.project("platform_verdict", project_id, solved=False, flag=flag, verdict=verdict)
        return {"solved": solved, "verdict": verdict}

    def _complete(self, project_id: str, flag: str) -> None:
        """The platform accepted: now (and only now) the project completes."""
        state = self.state
        if state.orchestrator is not None:
            try:
                state.orchestrator.diamond.draw_completion(project_id)
            except Exception as exc:
                state.logger.project(
                    "completion_graph_failed", project_id, error=f"{type(exc).__name__}: {exc}"
                )
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row is None:
                return
            title = row["title"]
            graph_store.add_broadcast(conn, project_id, title, flag)
        try:
            self.lifecycle.transition(project_id, "completed")
        except LifecycleError:
            with state.db.connect() as conn:
                graph_store.set_status(conn, project_id, "completed")
        if state.orchestrator is not None:
            state.orchestrator.mark_completed(project_id)
        else:
            with state.db.connect() as conn:
                graph_store.set_runtime_phase(conn, project_id, "completed")

    def _feedback(self, project_id: str, title: str, flag: str, verdict: str) -> None:
        """Feed the rejection back through both channels members read."""
        verdict_l = (verdict or "").lower()
        try:
            if "flag should be" in verdict_l or "format" in verdict_l:
                self.state.memory.add(
                    "lessons",
                    "Platform flag format feedback",
                    (
                        f"Platform rejected flag {flag!r} for {title!r}: {verdict}. "
                        "Honor the platform's expected flag format exactly; when the "
                        "material yields a different prefix, rewrite the prefix."
                    ),
                    tags=["flag-format", "submission", "ret2shell"],
                    project_id=project_id,
                    source="verdict",
                )
            else:
                self.state.memory.add(
                    "lessons",
                    "Rejected flag submission",
                    (
                        f"Platform judge REJECTED the flag {flag!r} for challenge {title!r} "
                        f"(verdict: {verdict}). Never submit this exact flag again. Re-derive "
                        "the complete flag from the challenge material instead: inspect EVERY "
                        "file and resource (including binary XML such as AndroidManifest.xml, "
                        "dex strings, config files, and any encoded blob), and beware truncated "
                        "or misordered fragment assembly — recombine all fragments and verify "
                        "the result reads as a coherent sentence before reporting."
                    ),
                    tags=["rejected-flag", "submission", "ret2shell"],
                    project_id=project_id,
                    source="verdict",
                )
        except Exception as exc:
            self.state.logger.project(
                "verdict_feedback_failed", project_id, error=f"{type(exc).__name__}: {exc}"
            )
        try:
            with self.state.db.connect() as conn:
                graph_store.create_hint(
                    conn,
                    project_id,
                    f"Platform rejected flag {flag!r}: {verdict or 'wrong flag'}. "
                    "Do not resubmit it; re-derive the complete flag from all evidence.",
                    "verdict",
                )
        except Exception as exc:
            self.state.logger.project(
                "verdict_hint_failed", project_id, error=f"{type(exc).__name__}: {exc}"
            )

    def _reopen(self, project_id: str, title: str, flag: str, verdict: str) -> None:
        """Platform said no: purge stale solution memories and reopen."""
        try:
            self.state.memory.delete_by_project(
                project_id,
                categories=_REOPEN_PURGE_CATEGORIES,
                keep_tags=_REOPEN_KEEP_TAGS,
            )
        except Exception as exc:
            self.state.logger.project(
                "verdict_memory_purge_failed", project_id, error=f"{type(exc).__name__}: {exc}"
            )
        with self.state.db.connect() as conn:
            graph_store.set_status(conn, project_id, "stopped")
            graph_store.set_runtime_phase(conn, project_id, "stopped")
            graph_store.clear_reason(conn, project_id)
        self._retry_not_before.pop((project_id, flag), None)
        self.state.logger.project(
            "reopened_after_rejection", project_id, title=title, flag=flag, verdict=verdict
        )

    # ---- ledger helpers ----

    def _record(
        self,
        project_id: str,
        flag: str,
        *,
        status: str,
        verdict: str | None = None,
        submission_id: str | None = None,
    ) -> None:
        with self.state.db.connect() as conn:
            graph_store.update_submission(
                conn,
                project_id,
                flag,
                status=status,
                verdict=verdict,
                submission_id=submission_id,
                bump_attempts=True,
            )

    def _bump_attempts(self, project_id: str, flag: str, runtime, *, verdict: str) -> int:
        with self.state.db.connect() as conn:
            graph_store.update_submission(conn, project_id, flag, verdict=verdict, bump_attempts=True)
            row = graph_store.get_submission(conn, project_id, flag)
        return row["attempts"] if row else 0
