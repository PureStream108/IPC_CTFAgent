"""Start a CompetitionRun the minute a takeable challenge appears.

A live run already re-syncs its platform on a timer, so it picks up challenges
that are published while it is running.  What the run cannot do is exist before
the first challenge it is willing to take: :meth:`CompetitionService.start`
refuses a workflow whose preflight is empty, and an ``attachments_only``
workflow filters every container challenge out.  During a live match whose
first questions all need a team container, that leaves the operator parked on
the page waiting to press Start by hand.

:class:`PlatformWatcher` closes that gap.  Once per interval it lists the
challenges of every watched attachment-only workflow and, as soon as at least
one unsolved challenge is visible, starts the run.  From that moment the run's
own sync loop owns discovery, attachment staging and assignment, so this thread
does nothing but read the platform until the run ends.

The watcher never starts a second run while one is active, never selects a
subset of challenges (a ``select`` run would freeze its challenge set and
ignore everything published later), and never touches a workflow that was not
explicitly watched or that does not declare ``attachments_only``.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from typing import Any

from backend.competition.store import CompetitionConflict
from backend.ops.models import PlatformWorkflowSpec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PlatformWatcher:
    """Poll a platform for takeable challenges and open the run for them."""

    def __init__(
        self,
        service,
        *,
        interval: float = 60.0,
        workflow_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.service = service
        self.interval = max(10.0, float(interval))
        self.workflow_ids = [
            str(item).strip() for item in workflow_ids if str(item).strip()
        ]
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.ticks = 0
        self.starts = 0
        self.refreshes = 0
        self.last_tick_at = ""
        self.last_action = "idle"
        self.last_error = ""
        self._logged_error = ""

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="ipc-platform-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=15)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self._log(f"watch tick raised {type(exc).__name__}: {exc}")
            self._wake.wait(self.interval)
            self._wake.clear()

    # -- one pass -------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Run one discovery pass and return a small, log-safe summary."""

        if not self._tick_lock.acquire(blocking=False):
            return {"ok": True, "action": "busy", "ticks": self.ticks}
        try:
            with self._state_lock:
                self.ticks += 1
                self.last_tick_at = _now()
            try:
                action, started, refreshed = self._pass()
            except Exception as exc:  # noqa: BLE001 - one bad tick is not fatal
                message = f"{type(exc).__name__}: {exc}"
                with self._state_lock:
                    self.last_error = message
                    self.last_action = "error"
                self._log_once("watch tick failed: " + message)
                return {"ok": False, "action": "error", "error": message}
            with self._state_lock:
                self.last_error = ""
                self.last_action = action
                self.starts += started
                self.refreshes += refreshed
            self._logged_error = ""
            return {"ok": True, "action": action, "ticks": self.ticks}
        finally:
            self._tick_lock.release()

    def _pass(self) -> tuple[str, int, int]:
        active = self.service.store.active_run()
        if active is not None:
            workflow_id = str(active.get("workflow_id") or "")
            if not self._watched(workflow_id):
                return f"run {active['id']} owns another workflow", 0, 0
            if str(active.get("status") or "") != "running":
                return f"run {active['id']} is {active.get('status')}", 0, 0
            try:
                result = self.service.refresh(active["id"])
            except CompetitionConflict:
                # The coordinator is mid-tick; the run is still being polled.
                return f"run {active['id']} is being refreshed", 0, 0
            summary = (
                f"run {active['id']}: {result.get('challenge_count', 0)} visible, "
                f"{result.get('new_count', 0)} new"
            )
            return (summary, 0, 1)
        for workflow in self._workflows():
            platform = self.service.platform_for_workflow(workflow["id"])
            pending = [item for item in platform.challenges() if not item.solved]
            if not pending:
                continue
            snapshot = self.service.start(
                workflow["id"], self._idempotency_key(workflow["id"])
            )
            run = snapshot.get("run") if isinstance(snapshot, dict) else None
            run_id = (run or snapshot or {}).get("id", "?")
            titles = ", ".join(item.title for item in pending[:3])
            self._log(
                f"started run {run_id} for {workflow['id']}"
                f" with {len(pending)} challenge(s): {titles}"
            )
            return f"started run {run_id} ({len(pending)} waiting)", 1, 0
        return "no takeable challenge", 0, 0

    # -- helpers --------------------------------------------------------

    def _workflows(self) -> list[dict[str, Any]]:
        watched: list[dict[str, Any]] = []
        for workflow in self.service.ops.store.list_workflows():
            if str(workflow.get("status") or "") != "confirmed":
                continue
            spec = workflow.get("spec")
            if not isinstance(spec, PlatformWorkflowSpec):
                continue
            if not spec.challenges.attachments_only:
                continue
            if self.workflow_ids and workflow["id"] not in self.workflow_ids:
                continue
            watched.append(workflow)
        return watched

    def _watched(self, workflow_id: str) -> bool:
        if self.workflow_ids:
            return workflow_id in self.workflow_ids
        return any(
            workflow["id"] == workflow_id for workflow in self._workflows()
        )

    def _idempotency_key(self, workflow_id: str) -> str:
        """One key per workflow per hour, so a failed start retries cleanly."""

        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        return f"watch-{workflow_id}-{bucket}"

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "enabled": bool(self._thread is not None and self._thread.is_alive()),
                "interval_seconds": self.interval,
                "sync_seconds": getattr(self.service, "sync_interval", None),
                "workflows": list(self.workflow_ids),
                "ticks": self.ticks,
                "starts": self.starts,
                "refreshes": self.refreshes,
                "last_tick_at": self.last_tick_at,
                "last_action": self.last_action,
                "last_error": self.last_error,
            }

    def _log(self, message: str) -> None:
        print(f"[ipc-watch] {message}", file=sys.stderr, flush=True)

    def _log_once(self, message: str) -> None:
        if message == self._logged_error:
            return
        self._logged_error = message
        self._log(message)
