"""Graded response to a session that has stopped making progress (P5c).

The failure this exists for: a Member's seat lease was renewed by the
coordinator's tick, so a worker that had silently stopped kept its challenge
until the deadline. P5b made the worker certify itself; this decides what to do
once a session goes quiet anyway.

The response is graded rather than binary because the cost of being wrong is
asymmetric. Killing a Member that is thirty seconds into a legitimate three
minute provider turn destroys real work; waiting one extra interval on a
genuinely dead one costs almost nothing. So escalation runs:

    diagnose -> cancel the current turn -> hand the session to another Member

Each step is recorded before it acts, which is what makes a later postmortem
able to distinguish "we killed a working Member" from "the Member was gone".

One subtlety worth stating plainly, because it is the easiest thing to get
wrong here: competition writes are fenced by the **assignment** lease
(``competition_assignments.lease_owner``/``epoch``), while a handoff moves the
**writer** row. Advancing only one of them leaves the new Member unable to
write a single event. ``handoff_assignment`` therefore moves both inside one
transaction.
"""
from __future__ import annotations

from dataclasses import dataclass


DIAGNOSE_AFTER = 300
CANCEL_AFTER = 900
HANDOFF_AFTER = 1800

LEVELS = ("ok", "diagnose", "cancel", "handoff")


@dataclass(frozen=True)
class Verdict:
    """What the watchdog concluded about one session."""

    session_id: str
    level: str
    quiet_seconds: int
    reason: str

    @property
    def actionable(self) -> bool:
        return self.level != "ok"


def classify(
    health: dict,
    *,
    diagnose_after: int = DIAGNOSE_AFTER,
    cancel_after: int = CANCEL_AFTER,
    handoff_after: int = HANDOFF_AFTER,
) -> Verdict:
    """Grade one row of ``agent_session_health``.

    A session with a running job is never escalated past ``diagnose``: the job
    poll is itself proof the worker is turning, and a long brute force is the
    exact case P4a exists to support. Treating it as a stall would re-impose
    the ceiling P4a removed.
    """
    session_id = health["session_id"]
    quiet = int(health.get("quiet_seconds") or 0)
    running_jobs = int(health.get("running_jobs") or 0)

    if quiet < diagnose_after:
        return Verdict(session_id, "ok", quiet, "progressing")
    if running_jobs > 0:
        return Verdict(
            session_id, "diagnose", quiet,
            f"quiet for {quiet}s but {running_jobs} job(s) still running; "
            "long work is expected to look idle",
        )
    worker_live = health.get("worker_live")
    if worker_live is False and quiet >= cancel_after:
        return Verdict(
            session_id, "handoff", quiet,
            f"worker lease lapsed and no progress for {quiet}s",
        )
    if quiet >= handoff_after:
        return Verdict(
            session_id, "handoff", quiet,
            f"no progress for {quiet}s across every signal",
        )
    if quiet >= cancel_after:
        return Verdict(
            session_id, "cancel", quiet,
            f"no progress for {quiet}s; cancelling the current turn",
        )
    return Verdict(
        session_id, "diagnose", quiet, f"no progress for {quiet}s"
    )


def diagnosis(health: dict) -> dict:
    """The evidence behind a verdict, for the run event stream.

    Recorded even when no action is taken: "we looked and it was working" is
    the observation that makes a later stall interpretable.
    """
    return {
        "session_id": health["session_id"],
        "task_key": health.get("task_key"),
        "owner": health.get("owner"),
        "epoch": health.get("epoch"),
        "worker_live": health.get("worker_live"),
        "quiet_seconds": int(health.get("quiet_seconds") or 0),
        "last_event_at": str(health.get("last_event_at")),
        "last_event_kind": health.get("last_event_kind"),
        "current_turn": health.get("current_turn"),
        "turn_status": health.get("turn_status"),
        "current_turn_started_at": str(health.get("current_turn_started_at")),
        "running_jobs": int(health.get("running_jobs") or 0),
        "last_renewed_at": str(health.get("last_renewed_at")),
    }


class SessionWatchdog:
    """Classify every live session in a run and apply the graded response."""

    def __init__(
        self,
        agent_store,
        competition_store,
        *,
        diagnose_after: int = DIAGNOSE_AFTER,
        cancel_after: int = CANCEL_AFTER,
        handoff_after: int = HANDOFF_AFTER,
    ) -> None:
        self.agent_store = agent_store
        self.competition_store = competition_store
        self.diagnose_after = diagnose_after
        self.cancel_after = cancel_after
        self.handoff_after = handoff_after

    def verdicts(self, *, kind: str = "competition") -> list[Verdict]:
        rows = self.agent_store.health_for_scope(kind=kind)
        return [
            classify(
                row,
                diagnose_after=self.diagnose_after,
                cancel_after=self.cancel_after,
                handoff_after=self.handoff_after,
            )
            for row in rows
        ]

    def inspect(self, *, kind: str = "competition") -> list[tuple[Verdict, dict]]:
        """Return each actionable verdict with the evidence behind it."""
        rows = self.agent_store.health_for_scope(kind=kind)
        results: list[tuple[Verdict, dict]] = []
        for row in rows:
            verdict = classify(
                row,
                diagnose_after=self.diagnose_after,
                cancel_after=self.cancel_after,
                handoff_after=self.handoff_after,
            )
            if verdict.actionable:
                results.append((verdict, diagnosis(row)))
        return results
