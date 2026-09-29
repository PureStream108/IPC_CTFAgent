"""Short-transaction access to the unified agent session tables.

Two rules shape this module:

* ``agent_events`` is append-only.  A compaction records a derived summary in
  ``agent_compactions``; it never edits or deletes the events it summarizes, so
  the evidence chain of a task outlives every projection decision.
* Writing requires a live fence.  ``append_event`` validates
  ``agent_session_writers`` inside the same transaction as the insert, which is
  what makes a member handoff safe: ownership moves to a new worker and the
  epoch advances, and the previous owner's next write is rejected instead of
  interleaving into the same transcript.
"""
from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb


class SessionWriterLost(RuntimeError):
    """The caller no longer owns the session it tried to write to."""


class SessionConflict(ValueError):
    """The requested session transition contradicts a durable invariant."""


KINDS = ("ops", "competition", "project")
LINEAGE_REASONS = (
    "fork", "resume", "redirect", "member_handoff", "model_switch", "helper",
)
EVENT_KINDS = (
    "user_message", "provider_messages", "reasoning", "tool_call",
    "tool_result", "status", "error", "handoff", "loop_detected",
    # Transitional kinds still written by the competition runtime; Phase 2
    # replaces them with rows in agent_compactions.
    "context_compacted", "compression_failed",
)
_SECRET_KEYS = frozenset({
    "api_key", "password", "token", "access_token", "refresh_token",
    "client_secret", "secret_value",
})


def new_session_id() -> str:
    return uuid.uuid4().hex


def ops_task_key(ops_session_id: str) -> str:
    return f"ops:{ops_session_id}"


def competition_task_key(run_id: str, challenge_id: str | None) -> str:
    return f"competition:{run_id}:{challenge_id or '-'}"


def project_task_key(project_id: str) -> str:
    return f"project:{project_id}"


def redact_model_snapshot(value):
    """Drop credential values from a snapshot while keeping its shape.

    ``model_snapshot`` is durable and surfaces in the Ops console, so an API
    key must never reach it.  Presence is preserved as ``<key>_set`` because
    "was a key configured" is useful when diagnosing a session.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                if item:
                    result[f"{key}_set"] = True
                continue
            result[key] = redact_model_snapshot(item)
        return result
    if isinstance(value, list):
        return [redact_model_snapshot(item) for item in value]
    return value


class AgentSessionStore:
    """Every method is one short transaction; none spans a model call."""

    def __init__(self, db):
        self.db = db

    # ---------------------------------------------------------------- sessions

    def create_session(
        self,
        kind: str,
        task_key: str,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        lineage_reason: str | None = None,
        provider_session_id: str | None = None,
        model_snapshot: dict | None = None,
        token_budget: dict | None = None,
    ) -> dict:
        if kind not in KINDS:
            raise SessionConflict(f"unknown agent session kind: {kind}")
        if not task_key.strip():
            raise SessionConflict("task key is required")
        if lineage_reason is not None and lineage_reason not in LINEAGE_REASONS:
            raise SessionConflict(f"unknown lineage reason: {lineage_reason}")
        identifier = session_id or new_session_id()
        with self.db.connect() as connection:
            self._lock(connection, task_key)
            if parent_session_id is None:
                live = connection.execute(
                    """SELECT id FROM agent_sessions
                       WHERE task_key=%s AND state='active' AND parent_session_id IS NULL""",
                    (task_key,),
                ).fetchone()
                if live:
                    raise SessionConflict(
                        "task already has an active root session"
                    )
            return connection.execute(
                """INSERT INTO agent_sessions
                   (id,kind,task_key,parent_session_id,lineage_reason,
                    provider_session_id,model_snapshot,token_budget)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    identifier, kind, task_key, parent_session_id, lineage_reason,
                    provider_session_id or f"ipc-session-{uuid.uuid4().hex[:16]}",
                    Jsonb(redact_model_snapshot(model_snapshot or {})),
                    Jsonb(token_budget or {}),
                ),
            ).fetchone()

    def session(self, session_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE id=%s", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return row

    def active_session_for_task(self, task_key: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM agent_sessions
                   WHERE task_key=%s AND state='active' AND parent_session_id IS NULL""",
                (task_key,),
            ).fetchone()

    def sessions_for_task(self, task_key: str) -> list[dict]:
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM agent_sessions WHERE task_key=%s ORDER BY created_at,id",
                (task_key,),
            ).fetchall()

    def set_state(self, session_id: str, state: str) -> dict:
        if state not in {"active", "idle", "finished", "failed", "superseded"}:
            raise SessionConflict(f"unknown session state: {state}")
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_sessions SET state=%s,updated_at=now()
                   WHERE id=%s RETURNING *""",
                (state, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return row

    def save_checkpoint(self, session_id: str, checkpoint: dict) -> None:
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_sessions SET checkpoint=%s,updated_at=now()
                   WHERE id=%s RETURNING id""",
                (Jsonb(checkpoint), session_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)

    def record_provider_session(
        self, session_id: str, provider_session_id: str, state: str
    ) -> dict:
        if state not in {"unknown", "native", "degraded", "unsupported"}:
            raise SessionConflict(f"unknown provider session state: {state}")
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_sessions
                   SET provider_session_id=%s,provider_session_state=%s,updated_at=now()
                   WHERE id=%s RETURNING *""",
                (provider_session_id, state, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            return row

    def save_token_budget(self, session_id: str, budget: dict) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE agent_sessions SET token_budget=%s,updated_at=now() WHERE id=%s",
                (Jsonb(budget), session_id),
            )

    # ----------------------------------------------------------------- writers

    def claim_writer(
        self,
        session_id: str,
        worker_kind: str,
        owner: str,
        *,
        seconds: int = 60,
        takeover: bool = False,
    ) -> dict:
        """Register the first writer of a session, or resume as the same owner.

        ``takeover`` is for a runtime whose turns are already serialized
        elsewhere - Ops allows one active run per conversation - where the next
        run is a legitimate successor rather than a competing writer. It still
        advances the epoch, so a straggler from the previous run is fenced out.
        """
        if not owner or not worker_kind:
            raise SessionConflict("writer owner and kind are required")
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_session_writers WHERE session_id=%s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if existing is None:
                return connection.execute(
                    """INSERT INTO agent_session_writers
                       (session_id,worker_kind,owner,lease_expires_at)
                       VALUES (%s,%s,%s,now()+(%s*interval '1 second')) RETURNING *""",
                    (session_id, worker_kind, owner, max(1, seconds)),
                ).fetchone()
            if existing["owner"] == owner:
                return connection.execute(
                    """UPDATE agent_session_writers
                       SET lease_expires_at=now()+(%s*interval '1 second'),
                           last_renewed_at=now()
                       WHERE session_id=%s RETURNING *""",
                    (max(1, seconds), session_id),
                ).fetchone()
            if takeover:
                return connection.execute(
                    """UPDATE agent_session_writers
                       SET owner=%s,worker_kind=%s,epoch=epoch+1,
                           lease_expires_at=now()+(%s*interval '1 second'),
                           last_renewed_at=now()
                       WHERE session_id=%s RETURNING *""",
                    (owner, worker_kind, max(1, seconds), session_id),
                ).fetchone()
            raise SessionWriterLost(
                "session is owned by another writer; take it over with handoff()"
            )

    def renew_writer(
        self, session_id: str, owner: str, epoch: int, *, seconds: int = 60
    ) -> bool:
        with self.db.connect() as connection:
            return bool(connection.execute(
                """UPDATE agent_session_writers
                   SET lease_expires_at=now()+(%s*interval '1 second'),last_renewed_at=now()
                   WHERE session_id=%s AND owner=%s AND epoch=%s
                     AND lease_expires_at>now() RETURNING session_id""",
                (max(1, seconds), session_id, owner, epoch),
            ).fetchone())

    def writer(self, session_id: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM agent_session_writers WHERE session_id=%s",
                (session_id,),
            ).fetchone()

    def handoff(
        self,
        session_id: str,
        new_owner: str,
        *,
        worker_kind: str | None = None,
        reason: str = "member_handoff",
        seconds: int = 60,
        require_expired: bool = False,
    ) -> dict:
        """Move write ownership of a session without breaking its transcript.

        The epoch advances in the same transaction as the ``handoff`` event, so
        the previous owner's in-flight writes are fenced out rather than
        interleaved.  ``require_expired`` restricts the takeover to a dead
        worker; a deliberate handoff of a live seat passes False.
        """
        if not new_owner:
            raise SessionConflict("handoff requires a new owner")
        if reason not in LINEAGE_REASONS:
            raise SessionConflict(f"unknown handoff reason: {reason}")
        with self.db.connect() as connection:
            current = connection.execute(
                "SELECT * FROM agent_session_writers WHERE session_id=%s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if current is None:
                raise KeyError(session_id)
            if require_expired and current["lease_expires_at"] is not None:
                live = connection.execute(
                    """SELECT 1 FROM agent_session_writers
                       WHERE session_id=%s AND lease_expires_at>now()""",
                    (session_id,),
                ).fetchone()
                if live:
                    raise SessionWriterLost("current writer lease is still live")
            row = connection.execute(
                """UPDATE agent_session_writers
                   SET owner=%s,worker_kind=COALESCE(%s,worker_kind),epoch=epoch+1,
                       lease_expires_at=now()+(%s*interval '1 second'),last_renewed_at=now()
                   WHERE session_id=%s RETURNING *""",
                (new_owner, worker_kind, max(1, seconds), session_id),
            ).fetchone()
            self._append_locked(
                connection,
                session_id,
                f"handoff:{row['epoch']}",
                "handoff",
                {
                    "previous_owner": current["owner"],
                    "owner": new_owner,
                    "epoch": row["epoch"],
                    "reason": reason,
                },
                turn=None,
            )
            return row

    # ------------------------------------------------------------------ events

    def append_event(
        self,
        session_id: str,
        event_key: str,
        kind: str,
        payload: dict[str, Any],
        *,
        owner: str,
        epoch: int,
        turn: int | None = None,
    ) -> dict:
        """Append one event, fenced on the caller still owning the session."""
        if kind not in EVENT_KINDS:
            raise SessionConflict(f"unknown agent event kind: {kind}")
        with self.db.connect() as connection:
            writer = connection.execute(
                """SELECT session_id FROM agent_session_writers
                   WHERE session_id=%s AND owner=%s AND epoch=%s
                     AND lease_expires_at>now() FOR UPDATE""",
                (session_id, owner, epoch),
            ).fetchone()
            if writer is None:
                raise SessionWriterLost("session writer lost its lease")
            return self._append_locked(
                connection, session_id, event_key, kind, payload, turn=turn
            )

    @staticmethod
    def _append_locked(
        connection,
        session_id: str,
        event_key: str,
        kind: str,
        payload: dict[str, Any],
        *,
        turn: int | None,
    ) -> dict:
        session = connection.execute(
            "SELECT next_sequence FROM agent_sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        existing = connection.execute(
            "SELECT * FROM agent_events WHERE session_id=%s AND event_key=%s",
            (session_id, event_key),
        ).fetchone()
        if existing is not None:
            # Replaying the identical event after a restart is the intended
            # path; the same key with different content means two writers
            # disagree about history.
            if existing["kind"] != kind or existing["payload"] != payload:
                raise SessionConflict("event key reused with different content")
            return existing
        row = connection.execute(
            """INSERT INTO agent_events
               (session_id,sequence,turn,event_key,kind,payload)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (session_id, session["next_sequence"], turn, event_key, kind, Jsonb(payload)),
        ).fetchone()
        connection.execute(
            "UPDATE agent_sessions SET next_sequence=next_sequence+1,updated_at=now() WHERE id=%s",
            (session_id,),
        )
        return row

    def events(
        self, session_id: str, after: int = 0, limit: int = 200
    ) -> list[dict]:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM agent_events WHERE session_id=%s AND sequence>%s
                   ORDER BY sequence LIMIT %s""",
                (session_id, max(0, after), max(1, min(limit, 500))),
            ).fetchall()

    def events_after_event_id(
        self, session_id: str, after_event_id: int = 0, limit: int = 200
    ) -> list[dict]:
        """Read by the globally monotonic id, for SSE cursors."""
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM agent_events WHERE session_id=%s AND event_id>%s
                   ORDER BY event_id LIMIT %s""",
                (session_id, max(0, after_event_id), max(1, min(limit, 500))),
            ).fetchall()

    # ------------------------------------------------------------------- turns

    def start_turn(
        self, session_id: str, turn: int, owner: str, epoch: int
    ) -> dict:
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM agent_turns WHERE session_id=%s AND turn=%s",
                (session_id, turn),
            ).fetchone()
            if existing is not None:
                return existing
            return connection.execute(
                """INSERT INTO agent_turns (session_id,turn,owner,epoch,status)
                   VALUES (%s,%s,%s,%s,'running') RETURNING *""",
                (session_id, turn, owner, epoch),
            ).fetchone()

    def finish_turn(
        self,
        session_id: str,
        turn: int,
        status: str,
        *,
        usage: dict | None = None,
    ) -> dict:
        if status not in {"running", "completed", "cancelled", "failed", "truncated"}:
            raise SessionConflict(f"unknown turn status: {status}")
        counts = usage or {}
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE agent_turns SET status=%s,
                       prompt_tokens=COALESCE(%s,prompt_tokens),
                       output_tokens=COALESCE(%s,output_tokens),
                       reasoning_tokens=COALESCE(%s,reasoning_tokens),
                       finished_at=CASE WHEN %s='running' THEN NULL ELSE now() END
                   WHERE session_id=%s AND turn=%s RETURNING *""",
                (
                    status,
                    counts.get("prompt_tokens"),
                    counts.get("output_tokens"),
                    counts.get("reasoning_tokens"),
                    status,
                    session_id,
                    turn,
                ),
            ).fetchone()
            if row is None:
                raise KeyError((session_id, turn))
            return row

    def turns(self, session_id: str, *, limit: int = 200) -> list[dict]:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM agent_turns WHERE session_id=%s
                   ORDER BY turn DESC LIMIT %s""",
                (session_id, max(1, min(limit, 500))),
            ).fetchall()

    # -------------------------------------------------------------- compaction

    def record_compaction(
        self,
        session_id: str,
        up_to_sequence: int,
        summary: dict,
        *,
        token_estimate: int | None = None,
        generated_by: dict | None = None,
    ) -> dict:
        """Record a summary of events up to a sequence, without deleting them."""
        with self.db.connect() as connection:
            return connection.execute(
                """INSERT INTO agent_compactions
                   (session_id,up_to_sequence,summary,token_estimate,generated_by)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (
                    session_id, up_to_sequence, Jsonb(summary), token_estimate,
                    Jsonb(generated_by or {}),
                ),
            ).fetchone()

    def latest_compaction(self, session_id: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM agent_compactions WHERE session_id=%s
                   ORDER BY up_to_sequence DESC, compaction_id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()

    @staticmethod
    def _lock(connection, task_key: str) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"agent-task:{task_key}",)
        )
