from __future__ import annotations

import uuid
import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from backend.core.config import MEMBER_NAMES
from backend.platform.verdict import cooldown_seconds


class CompetitionConflict(ValueError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


_SECRET_KEYS = {
    "api_key", "password", "token", "access_token", "refresh_token",
    "client_secret", "secret_value",
}


def _redact_snapshot(value):
    """Copy JSON-compatible snapshot data without returning credential values."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                if item:
                    result[f"{key}_set"] = True
                continue
            result[key] = _redact_snapshot(item)
        return result
    if isinstance(value, list):
        return [_redact_snapshot(item) for item in value]
    return value


class CompetitionStore:
    """Short transactions only: callers perform network/model work outside.

    A database-wide advisory lock serializes slot/identity transitions. It is
    deliberately not held during model calls, downloads, or platform requests.
    """

    def __init__(self, db):
        self.db = db

    def claim_engine_lease(
        self, project_id: str, kind: str, owner: str, *, seconds: int = 60
    ) -> dict | None:
        """Claim the project-level legacy/competition scheduler fence."""
        from backend.core.engine_lease import claim_engine_lease

        return claim_engine_lease(
            self.db, project_id, kind, owner, seconds=seconds
        )

    def renew_engine_lease(
        self, project_id: str, kind: str, owner: str, *, epoch: int,
        seconds: int = 60
    ) -> bool:
        from backend.core.engine_lease import renew_engine_lease

        return renew_engine_lease(
            self.db, project_id, kind, owner, epoch=epoch, seconds=seconds
        )

    def release_engine_lease(
        self, project_id: str, kind: str, owner: str, *, epoch: int
    ) -> bool:
        from backend.core.engine_lease import release_engine_lease

        return release_engine_lease(self.db, project_id, kind, owner, epoch=epoch)

    def release_engine_leases_for_owner(
        self, kind: str, owner: str, *, epochs: dict[str, int]
    ) -> int:
        from backend.core.engine_lease import release_engine_leases_for_owner

        return release_engine_leases_for_owner(self.db, kind, owner, epochs=epochs)

    def engine_lease(self, project_id: str) -> dict | None:
        from backend.core.engine_lease import engine_lease

        return engine_lease(self.db, project_id)

    @staticmethod
    def _lock(connection):
        connection.execute("SELECT pg_advisory_xact_lock(71342691)")

    def create_run(self, workflow_id: str, identity_key: str, idempotency_key: str, snapshot: dict) -> dict:
        if not identity_key.strip() or not idempotency_key.strip():
            raise ValueError("identity and idempotency keys are required")
        with self.db.connect() as connection:
            self._lock(connection)
            existing = connection.execute(
                "SELECT * FROM competition_runs WHERE idempotency_key = %s", (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["workflow_id"] != workflow_id or existing["identity_key"] != identity_key:
                    raise CompetitionConflict("idempotency key belongs to another run request")
                return existing
            active = connection.execute(
                "SELECT id FROM competition_runs WHERE status NOT IN ('finished','stopped')",
            ).fetchone()
            if active:
                raise CompetitionConflict("another competition is active")
            return connection.execute(
                """INSERT INTO competition_runs
                   (id,workflow_id,identity_key,idempotency_key,config_snapshot)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (new_id(), workflow_id, identity_key, idempotency_key, Jsonb(snapshot)),
            ).fetchone()

    def run_by_idempotency(self, idempotency_key: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM competition_runs WHERE idempotency_key=%s",
                (idempotency_key,),
            ).fetchone()

    def register_challenge(self, run_id: str, external_id: str, metadata: dict) -> dict:
        if not external_id.strip():
            raise ValueError("external id is required")
        with self.db.connect() as connection:
            self._lock(connection)
            run = connection.execute("SELECT * FROM competition_runs WHERE id=%s", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            existing = connection.execute(
                """SELECT c.* FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s AND c.external_id=%s FOR UPDATE""",
                (run_id, external_id),
            ).fetchone()
            if existing is not None:
                row = connection.execute(
                    """UPDATE competition_challenges SET metadata=metadata || %s
                       WHERE id=%s RETURNING *""",
                    (Jsonb(metadata), existing["id"]),
                ).fetchone()
            else:
                row = connection.execute(
                    """INSERT INTO competition_challenges
                       (id,identity_key,external_id,run_id,metadata)
                       VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                    (new_id(), run["identity_key"], external_id, run_id, Jsonb(metadata)),
                ).fetchone()
            connection.execute(
                """INSERT INTO competition_run_challenges (run_id,challenge_id)
                   VALUES (%s,%s) ON CONFLICT DO NOTHING""", (run_id, row["id"]),
            )
            return row

    def transition_run(
        self, run_id: str, status: str, *, revision: int,
        permit_active: bool = False,
    ) -> dict:
        allowed = {
            "preflight": {"importing", "blocked", "stopped"},
            "importing": {"running", "blocked", "paused", "stopped"},
            "running": {"paused", "blocked", "draining", "stopped"},
            "paused": {"running", "blocked", "stopped"},
            "blocked": {"preflight", "importing", "running", "paused", "stopped"},
            "draining": {"finished", "stopped"},
        }
        with self.db.connect() as connection:
            self._lock(connection)
            run = connection.execute("SELECT * FROM competition_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            if run["revision"] != revision or status not in allowed.get(run["status"], set()):
                raise CompetitionConflict("stale revision or invalid run transition")
            if status in {"finished", "stopped"} and not permit_active:
                active = connection.execute(
                    "SELECT 1 FROM competition_assignments WHERE run_id=%s AND released_at IS NULL", (run_id,),
                ).fetchone()
                if active:
                    raise CompetitionConflict("cancel and release all assignments before finishing the run")
            return connection.execute(
                """UPDATE competition_runs SET status=%s,revision=revision+1,updated_at=now()
                   WHERE id=%s RETURNING *""", (status, run_id),
            ).fetchone()

    def mark_ready(self, challenge_id: str) -> bool:
        """Call only after attachments and required instance are prepared."""
        with self.db.connect() as connection:
            return bool(connection.execute(
                """UPDATE competition_challenges SET state='ready'
                   WHERE id=%s AND state IN ('discovered','preparing','ready')
                   AND (deadline_at IS NULL OR deadline_at>now()) RETURNING id""", (challenge_id,),
            ).fetchone())

    def mark_external_solved(self, challenge_id: str) -> dict:
        """Record a platform-reported solved challenge without inventing a flag.

        ``solved`` here is an external fact.  It deliberately does not create
        a submission or WP job because no local Member candidate was verified.
        """
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE competition_challenges
                   SET state='solved', metadata=metadata || '{"external_solved": true}'::jsonb
                   WHERE id=%s AND state NOT IN ('solved','cancelled')
                   RETURNING *""",
                (challenge_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM competition_challenges WHERE id=%s", (challenge_id,)
                ).fetchone()
            if row is None:
                raise KeyError(challenge_id)
            return row

    def assign(self, run_id: str, challenge_id: str, member: str, owner: str, *, role: str = "primary") -> dict:
        if member not in MEMBER_NAMES or role not in {"primary", "helper"} or not owner:
            raise ValueError("invalid Member assignment")
        with self.db.connect() as connection:
            self._lock(connection)
            row = connection.execute(
                """SELECT c.*,r.status AS run_status FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   JOIN competition_runs r ON r.id=rc.run_id
                   WHERE c.id=%s AND r.id=%s FOR UPDATE OF c,r""", (challenge_id, run_id),
            ).fetchone()
            if not row or row["run_status"] != "running" or row["state"] not in {"ready", "solving"}:
                raise CompetitionConflict("challenge is not ready in an active run")
            expired = connection.execute(
                "SELECT 1 FROM competition_challenges WHERE id=%s AND deadline_at<=now()", (challenge_id,),
            ).fetchone()
            if expired:
                raise CompetitionConflict("challenge deadline has passed")
            positions = connection.execute(
                """SELECT position FROM competition_assignments
                   WHERE challenge_id=%s AND released_at IS NULL""", (challenge_id,),
            ).fetchall()
            if (role == "primary" and positions) or (role == "helper" and len(positions) != 1):
                raise CompetitionConflict("invalid primary/helper occupancy")
            if connection.execute(
                "SELECT 1 FROM competition_assignments WHERE member=%s AND released_at IS NULL", (member,),
            ).fetchone():
                raise CompetitionConflict("Member is already occupied")
            position = next(p for p in (1, 2) if p not in {x["position"] for x in positions})
            previous = connection.execute(
                """SELECT id FROM competition_sessions WHERE run_id=%s AND challenge_id=%s AND member=%s
                   ORDER BY created_at DESC LIMIT 1""", (run_id, challenge_id, member),
            ).fetchone()
            session_id = previous["id"] if previous else new_id()
            if previous is None:
                connection.execute(
                    "INSERT INTO competition_sessions (id,run_id,challenge_id,member) VALUES (%s,%s,%s,%s)",
                    (session_id, run_id, challenge_id, member),
                )
            connection.execute(
                """UPDATE competition_challenges SET state='solving',
                   first_assigned_at=COALESCE(first_assigned_at,now()),
                   deadline_at=COALESCE(deadline_at,now()+interval '5 hours') WHERE id=%s""", (challenge_id,),
            )
            return connection.execute(
                """INSERT INTO competition_assignments
                   (id,run_id,challenge_id,session_id,member,role,position,lease_owner,lease_expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()+interval '60 seconds') RETURNING *""",
                (new_id(), run_id, challenge_id, session_id, member, role, position, owner),
            ).fetchone()

    def heartbeat(self, assignment_id: str, owner: str, epoch: int) -> bool:
        with self.db.connect() as connection:
            return bool(connection.execute(
                """UPDATE competition_assignments a SET lease_expires_at=now()+interval '60 seconds'
                   FROM competition_challenges c,competition_runs r
                   WHERE a.id=%s AND a.lease_owner=%s AND a.epoch=%s
                   AND a.released_at IS NULL AND a.lease_expires_at>now()
                   AND c.id=a.challenge_id AND r.id=a.run_id AND r.status='running'
                   AND ((a.role<>'wp' AND c.deadline_at>now()) OR
                        (a.role='wp' AND a.wp_deadline_at>now())) RETURNING a.id""",
                (assignment_id, owner, epoch),
            ).fetchone())

    def recover_assignment(self, assignment_id: str, owner: str) -> dict:
        """Take over an expired lease without replacing the challenge session."""
        if not owner:
            raise ValueError("owner is required")
        with self.db.connect() as connection:
            self._lock(connection)
            row = connection.execute(
                """UPDATE competition_assignments a SET lease_owner=%s,epoch=epoch+1,
                   lease_expires_at=now()+interval '60 seconds'
                   FROM competition_challenges c,competition_runs r
                   WHERE a.id=%s AND a.released_at IS NULL AND a.lease_expires_at<=now()
                   AND c.id=a.challenge_id AND c.deadline_at>now()
                   AND r.id=a.run_id AND r.status='running' AND a.role<>'wp' RETURNING a.*""",
                (owner, assignment_id),
            ).fetchone()
            if row is None:
                raise CompetitionConflict("assignment is live, expired, or not resumable")
            return row

    def release(self, assignment_id: str, owner: str, epoch: int) -> bool:
        with self.db.connect() as connection:
            return bool(connection.execute(
                """UPDATE competition_assignments SET released_at=now(),epoch=epoch+1
                   WHERE id=%s AND lease_owner=%s AND epoch=%s AND released_at IS NULL RETURNING id""",
                (assignment_id, owner, epoch),
            ).fetchone())

    def append_event(self, session_id: str, event_key: str, kind: str, payload: dict[str, Any],
                     *, assignment_id: str, owner: str, epoch: int) -> dict:
        with self.db.connect() as connection:
            assignment = connection.execute(
                """SELECT a.id FROM competition_assignments a
                   JOIN competition_challenges c ON c.id=a.challenge_id
                   JOIN competition_runs r ON r.id=a.run_id
                   WHERE a.id=%s AND a.session_id=%s AND a.lease_owner=%s AND a.epoch=%s
                   AND a.released_at IS NULL AND a.lease_expires_at>now() AND r.status='running'
                   AND ((a.role<>'wp' AND c.deadline_at>now()) OR
                        (a.role='wp' AND a.wp_deadline_at>now())) FOR UPDATE OF a""",
                (assignment_id, session_id, owner, epoch),
            ).fetchone()
            if not assignment:
                raise CompetitionConflict("session writer lost its lease")
            session = connection.execute(
                "SELECT next_sequence FROM competition_sessions WHERE id=%s FOR UPDATE", (session_id,),
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM competition_session_events WHERE session_id=%s AND event_key=%s",
                (session_id, event_key),
            ).fetchone()
            if existing:
                if existing["kind"] != kind or existing["payload"] != payload:
                    raise CompetitionConflict("event key reused with different content")
                return existing
            row = connection.execute(
                """INSERT INTO competition_session_events (session_id,sequence,event_key,kind,payload)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (session_id, session["next_sequence"], event_key, kind, Jsonb(payload)),
            ).fetchone()
            connection.execute("UPDATE competition_sessions SET next_sequence=next_sequence+1 WHERE id=%s", (session_id,))
            return row

    def events(self, session_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM competition_session_events WHERE session_id=%s AND sequence>%s
                   ORDER BY sequence LIMIT %s""", (session_id, max(0, after), max(1, min(limit, 500))),
            ).fetchall()

    def save_checkpoint(self, assignment: dict, checkpoint: dict) -> None:
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE competition_sessions s SET checkpoint=%s FROM competition_assignments a
                   WHERE a.id=%s AND a.session_id=s.id AND a.lease_owner=%s AND a.epoch=%s
                   AND a.lease_expires_at>now() AND a.released_at IS NULL RETURNING s.id""",
                (Jsonb(checkpoint), assignment["id"], assignment["lease_owner"], assignment["epoch"]),
            ).fetchone()
            if not row:
                raise CompetitionConflict("checkpoint writer lost its lease")

    def session(self, session_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM competition_sessions WHERE id=%s", (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            return row

    def queue_candidate(self, assignment: dict, flag: str, evidence: str, generation: int) -> dict:
        flag = flag.strip()
        if not flag or len(flag)>4096 or not evidence.strip() or len(evidence)>12000:
            raise ValueError("one Flag candidate and its derivation evidence are required")
        with self.db.connect() as connection:
            self._lock(connection)
            row = connection.execute(
                """SELECT c.* FROM competition_challenges c JOIN competition_assignments a ON a.challenge_id=c.id
                   JOIN competition_runs r ON r.id=a.run_id
                   WHERE a.id=%s AND a.lease_owner=%s AND a.epoch=%s AND a.released_at IS NULL
                   AND a.lease_expires_at>now() AND c.deadline_at>now() AND c.state='solving'
                   AND r.status='running' AND a.role<>'wp' FOR UPDATE OF c""",
                (assignment["id"], assignment["lease_owner"], assignment["epoch"]),
            ).fetchone()
            if not row or row["instance_generation"] != generation:
                raise CompetitionConflict("candidate belongs to an inactive assignment or stale instance")
            return connection.execute(
                """INSERT INTO competition_submissions
                   (id,challenge_id,run_id,session_id,member,candidate,candidate_hash,evidence,instance_generation)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (challenge_id,candidate_hash) DO UPDATE SET candidate_hash=EXCLUDED.candidate_hash
                   RETURNING id,status""",
                (new_id(), row["id"], assignment["run_id"], assignment["session_id"], assignment["member"], flag,
                 hashlib.sha256(flag.encode()).hexdigest(), evidence, generation),
            ).fetchone()

    def queue_recovered_candidate(
        self, run_id: str, challenge_id: str, flag: str, evidence: str
    ) -> dict:
        """Bridge a durable candidate produced by the legacy Project runtime.

        The latest solver assignment determines authorship and session.  This
        path is coordinator-only; public callers must use ``queue_candidate``
        so their live lease and instance generation are fenced.
        """
        normalized = flag.strip()
        if not normalized or not evidence.strip():
            raise ValueError("candidate and evidence are required")
        with self.db.connect() as connection:
            self._lock(connection)
            challenge = connection.execute(
                """SELECT c.* FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE c.id=%s AND rc.run_id=%s FOR UPDATE OF c""",
                (challenge_id, run_id),
            ).fetchone()
            if challenge is None or challenge["state"] not in {"assigned", "solving"}:
                raise CompetitionConflict("challenge is not accepting candidates")
            assignment = connection.execute(
                """SELECT * FROM competition_assignments
                   WHERE run_id=%s AND challenge_id=%s AND role<>'wp'
                   ORDER BY released_at NULLS FIRST, id DESC LIMIT 1""",
                (run_id, challenge_id),
            ).fetchone()
            if assignment is None:
                raise CompetitionConflict("candidate has no solver session")
            return connection.execute(
                """INSERT INTO competition_submissions
                   (id,challenge_id,run_id,session_id,member,candidate,candidate_hash,
                    evidence,instance_generation)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (challenge_id,candidate_hash)
                   DO UPDATE SET candidate_hash=EXCLUDED.candidate_hash
                   RETURNING id,status""",
                (
                    new_id(), challenge_id, run_id, assignment["session_id"],
                    assignment["member"], normalized,
                    hashlib.sha256(normalized.encode()).hexdigest(), evidence,
                    challenge["instance_generation"],
                ),
            ).fetchone()
    def claim_submission(self, run_id: str) -> dict | None:
        with self.db.connect() as connection:
            self._lock(connection)
            # Never reset submitting/unknown to queued after a process crash.
            return connection.execute(
                """UPDATE competition_submissions SET status='submitting',submitted_at=now()
                   WHERE id=(SELECT s.id FROM competition_submissions s
                     JOIN competition_challenges c ON c.id=s.challenge_id
                     JOIN competition_runs r ON r.id=s.run_id
                     WHERE s.run_id=%s AND s.status='queued' AND r.status='running'
                     AND c.state='solving' AND c.deadline_at>now()
                     AND c.instance_generation=s.instance_generation
                     AND (c.next_submission_at IS NULL OR c.next_submission_at<=now())
                     AND NOT EXISTS (SELECT 1 FROM competition_submissions other
                       WHERE other.challenge_id=s.challenge_id
                         AND other.status IN ('submitting','pending','auth_required'))
                     ORDER BY s.created_at LIMIT 1) RETURNING *""", (run_id,),
            ).fetchone()

    def finish_submission(self, submission_id: str, result: dict) -> dict:
        status = result["verdict"]
        # A pending response without a durable platform id cannot be queried
        # after a restart.  Keep it visibly uncertain instead of creating an
        # unresolvable pending row.
        if status == "pending" and not result.get("submission_id"):
            status = "unknown"
        if status not in {"correct", "wrong", "pending", "unknown", "rate_limited", "auth_required", "rejected"}:
            raise ValueError("unknown verdict state")
        with self.db.connect() as connection:
            self._lock(connection)
            row = connection.execute("SELECT * FROM competition_submissions WHERE id=%s FOR UPDATE", (submission_id,)).fetchone()
            if not row:
                raise KeyError(submission_id)
            if row["status"] in {"correct", "wrong"}:
                return row
            challenge = connection.execute("SELECT * FROM competition_challenges WHERE id=%s FOR UPDATE", (row["challenge_id"],)).fetchone()
            connection.execute(
                """UPDATE competition_submissions SET status=%s,verdict=%s,platform_submission_id=%s WHERE id=%s""",
                (status, Jsonb(result), result.get("submission_id"), submission_id),
            )
            if status == "wrong" and challenge["state"] != "solved":
                wrong_count = challenge["wrong_count"] + 1
                connection.execute(
                    """UPDATE competition_challenges SET wrong_count=%s,
                       next_submission_at=now()+(%s * interval '1 second') WHERE id=%s""",
                    (wrong_count, cooldown_seconds(wrong_count), challenge["id"]),
                )
            elif status == "rate_limited":
                # Conservatively apply platform rate limiting to the entire run.
                connection.execute(
                    """UPDATE competition_challenges c SET next_submission_at=GREATEST(
                       COALESCE(next_submission_at,now()),now()+(%s*interval '1 second'))
                       FROM competition_run_challenges rc WHERE c.id=rc.challenge_id AND rc.run_id=%s""",
                    (max(1, int(result.get("retry_after") or 60)), row["run_id"]),
                )
                connection.execute("UPDATE competition_submissions SET status='queued' WHERE id=%s", (submission_id,))
            elif status == "correct" and challenge["state"] != "solved":
                connection.execute("UPDATE competition_challenges SET state='solved' WHERE id=%s", (challenge["id"],))
                connection.execute(
                    """INSERT INTO competition_wp_jobs (challenge_id,run_id,member,session_id)
                       VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (challenge["id"], row["run_id"], row["member"], row["session_id"]),
                )
            return {**row, "status": status}

    def reconcile_inflight_submissions(self, run_id: str, *, grace_seconds: int = 30) -> int:
        """Move abandoned ``submitting`` rows to queryable ``unknown``.

        The transition is intentionally conservative: a fresh request may
        still be in flight, so only rows older than the bounded grace period
        are reclaimed.  Unknown rows are never requeued blindly.
        """
        if grace_seconds < 0 or grace_seconds > 3600:
            raise ValueError("invalid submission reconciliation grace")
        with self.db.connect() as connection:
            cursor = connection.execute(
                """UPDATE competition_submissions
                   SET status='unknown', verdict=COALESCE(verdict, '{"verdict":"unknown","reason":"worker restarted"}'::jsonb)
                   WHERE run_id=%s AND status='submitting'
                     AND submitted_at < now()-(%s*interval '1 second')""",
                (run_id, grace_seconds),
            )
            return cursor.rowcount

    def get_run(self, run_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM competition_runs WHERE id=%s", (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            return row

    def active_run(self) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM competition_runs
                   WHERE status NOT IN ('finished','stopped')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM competition_runs ORDER BY created_at DESC LIMIT %s",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [
                {**row, "config_snapshot": _redact_snapshot(row.get("config_snapshot"))}
                for row in rows
            ]

    def append_run_event(self, run_id: str, kind: str, payload: dict | None = None) -> dict:
        if not kind or len(kind) > 120:
            raise ValueError("event kind is required")
        with self.db.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM competition_runs WHERE id=%s", (run_id,),
            ).fetchone():
                raise KeyError(run_id)
            return connection.execute(
                """INSERT INTO competition_run_events (run_id,kind,payload)
                   VALUES (%s,%s,%s) RETURNING *""",
                (run_id, kind, Jsonb(payload or {})),
            ).fetchone()

    def run_events(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        with self.db.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM competition_runs WHERE id=%s", (run_id,),
            ).fetchone():
                raise KeyError(run_id)
            return connection.execute(
                """SELECT * FROM competition_run_events
                   WHERE run_id=%s AND event_id>%s ORDER BY event_id LIMIT %s""",
                (run_id, max(0, after), max(1, min(limit, 1000))),
            ).fetchall()

    def observe_run(self, run_id: str, owner: str) -> dict:
        """Persist a compact, redacted health sample for a competition run.

        The sample is intentionally derived from durable rows rather than
        process-local counters, so it remains useful after a coordinator
        restart and can expose replay/lease pressure to operators.
        """
        if not owner:
            raise ValueError("observation owner is required")
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT id,next_sync_at FROM competition_runs WHERE id=%s",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            assignment_rows = connection.execute(
                """SELECT role,count(*) AS count FROM competition_assignments
                   WHERE run_id=%s AND released_at IS NULL GROUP BY role""",
                (run_id,),
            ).fetchall()
            assignment_counts = {row["role"]: int(row["count"]) for row in assignment_rows}
            pending_challenges = connection.execute(
                """SELECT count(*) AS count FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s AND c.state IN ('ready','assigned','solving')""",
                (run_id,),
            ).fetchone()["count"]
            submission_rows = connection.execute(
                """SELECT status,count(*) AS count FROM competition_submissions
                   WHERE run_id=%s GROUP BY status""",
                (run_id,),
            ).fetchall()
            submission_counts = {
                row["status"]: int(row["count"]) for row in submission_rows
            }
            wp_rows = connection.execute(
                """SELECT status,count(*) AS count FROM competition_wp_jobs
                   WHERE run_id=%s GROUP BY status""",
                (run_id,),
            ).fetchall()
            wp_counts = {row["status"]: int(row["count"]) for row in wp_rows}
            instance_rows = connection.execute(
                """SELECT state,count(*) AS count FROM competition_instances
                   WHERE run_id=%s GROUP BY state""",
                (run_id,),
            ).fetchall()
            instance_counts = {row["state"]: int(row["count"]) for row in instance_rows}
            replay_backlog = connection.execute(
                """SELECT count(*) AS count
                   FROM competition_messages m
                   LEFT JOIN competition_message_cursors cur
                     ON cur.session_id=(m.envelope->>'receiver_session_id')
                   WHERE m.run_id=%s AND m.sequence>COALESCE(cur.sequence,0)""",
                (run_id,),
            ).fetchone()["count"]
            artifact_bytes = connection.execute(
                """SELECT COALESCE(sum(a.size_bytes),0) AS bytes
                   FROM competition_artifacts a
                   JOIN competition_challenges c ON c.id=a.challenge_id
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s""",
                (run_id,),
            ).fetchone()["bytes"]
            engine_leases = connection.execute(
                """SELECT p.engine_kind,count(*) AS count
                   FROM projects p
                   JOIN competition_challenges c ON c.project_id=p.id
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s AND p.engine_owner IS NOT NULL
                     AND p.engine_lease_expires_at>now()
                   GROUP BY p.engine_kind""",
                (run_id,),
            ).fetchall()
            engine_counts = {
                row["engine_kind"] or "unknown": int(row["count"])
                for row in engine_leases
            }
            sync_lag = connection.execute(
                """SELECT GREATEST(0,EXTRACT(EPOCH FROM (now()-next_sync_at))) AS seconds
                   FROM competition_runs WHERE id=%s""",
                (run_id,),
            ).fetchone()["seconds"]
            payload = {
                "capacity": {
                    "total": len(MEMBER_NAMES),
                    "solving": assignment_counts.get("primary", 0),
                    "collaborating": assignment_counts.get("helper", 0),
                    "writing": assignment_counts.get("wp", 0),
                    "occupied": sum(assignment_counts.values()),
                },
                "pending_challenges": int(pending_challenges),
                "submissions": submission_counts,
                "wp": wp_counts,
                "instances": instance_counts,
                "replay_backlog": int(replay_backlog),
                "engine_leases": engine_counts,
                "sync_lag_seconds": round(float(sync_lag or 0), 3),
                "artifact_bytes": int(artifact_bytes or 0),
            }
            return connection.execute(
                """INSERT INTO competition_run_observations (run_id,owner,payload)
                   VALUES (%s,%s,%s) RETURNING *""",
                (run_id, owner, Jsonb(payload)),
            ).fetchone()

    def observations(self, run_id: str, *, limit: int = 100) -> list[dict]:
        with self.db.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM competition_runs WHERE id=%s", (run_id,)
            ).fetchone():
                raise KeyError(run_id)
            return connection.execute(
                """SELECT * FROM competition_run_observations
                   WHERE run_id=%s ORDER BY observation_id DESC LIMIT %s""",
                (run_id, max(1, min(limit, 1000))),
            ).fetchall()

    def challenges(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT c.* FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s ORDER BY c.external_id,c.id""",
                (run_id,),
            ).fetchall()

    def restorable_seats(self, run_id: str) -> list[dict]:
        """Released solver seats of a run, newest first per challenge/role.

        A paused run releases every seat.  Resuming restores the original
        Member<->challenge pairing so each solver continues its own durable
        session instead of being scattered onto unfamiliar challenges.
        """
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT DISTINCT ON (a.challenge_id, a.role) a.challenge_id, a.member, a.role
                     FROM competition_assignments a
                    WHERE a.run_id=%s AND a.released_at IS NOT NULL AND a.role<>'wp'
                    ORDER BY a.challenge_id, a.role DESC, a.id DESC""",
                (run_id,),
            ).fetchall()

    def assignments(self, run_id: str, *, active_only: bool = False) -> list[dict]:
        where = "AND a.released_at IS NULL" if active_only else ""
        with self.db.connect() as connection:
            return connection.execute(
                f"""SELECT a.* FROM competition_assignments a
                    WHERE a.run_id=%s {where} ORDER BY a.member,a.id""",
                (run_id,),
            ).fetchall()

    def run_snapshot(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        # Older runs may predate credential redaction.  Never echo sensitive
        # snapshot fields through a status endpoint, even before a migration
        # has scrubbed the persisted JSON.
        run = dict(run)
        snapshot = run.get("config_snapshot")
        if isinstance(snapshot, (dict, list)):
            run["config_snapshot"] = _redact_snapshot(snapshot)
        challenges = self.challenges(run_id)
        assignments = self.assignments(run_id, active_only=True)
        by_challenge: dict[str, list[dict]] = {}
        for assignment in assignments:
            by_challenge.setdefault(assignment["challenge_id"], []).append(assignment)
        challenge_views = []
        for challenge in challenges:
            item = dict(challenge)
            item["assignments"] = by_challenge.get(challenge["id"], [])
            challenge_views.append(item)
        solving = sum(a["role"] in {"primary", "helper"} for a in assignments)
        writing = sum(a["role"] == "wp" for a in assignments)
        return {
            "run": run,
            "challenges": challenge_views,
            "capacity": {
                "total": len(MEMBER_NAMES),
                "solving": solving,
                "writing": writing,
                "collaborating": sum(a["role"] == "helper" for a in assignments),
                "idle": max(0, len(MEMBER_NAMES) - len(assignments)),
            },
        }

    def member_snapshot(self, run_id: str) -> list[dict]:
        active = {row["member"]: row for row in self.assignments(run_id, active_only=True)}
        result = []
        for member in MEMBER_NAMES:
            assignment = active.get(member)
            result.append({
                "member": member,
                "state": "idle" if assignment is None else assignment["role"],
                "assignment": assignment,
            })
        return result

    def link_project(self, challenge_id: str, project_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE competition_challenges SET project_id=%s
                   WHERE id=%s AND (project_id IS NULL OR project_id=%s) RETURNING *""",
                (project_id, challenge_id, project_id),
            ).fetchone()
            if row is None:
                raise CompetitionConflict("challenge is already linked to another project")
            return row

    def challenge_for_project(self, run_id: str, project_id: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT c.* FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s AND c.project_id=%s""",
                (run_id, project_id),
            ).fetchone()

    def set_challenge_state(self, challenge_id: str, state: str) -> dict:
        allowed = {
            "discovered", "preparing", "ready", "assigned", "solving", "solved",
            "expired", "withdrawn", "cancelled", "blocked",
        }
        if state not in allowed:
            raise ValueError("invalid challenge state")
        with self.db.connect() as connection:
            row = connection.execute(
                "UPDATE competition_challenges SET state=%s WHERE id=%s RETURNING *",
                (state, challenge_id),
            ).fetchone()
            if row is None:
                raise KeyError(challenge_id)
            return row

    def claim_run_lease(self, run_id: str, owner: str, *, seconds: int = 30) -> bool:
        if not owner or seconds < 5 or seconds > 300:
            raise ValueError("invalid run lease")
        with self.db.connect() as connection:
            return bool(connection.execute(
                """UPDATE competition_runs SET lease_owner=%s,
                   lease_expires_at=now()+(%s*interval '1 second')
                   WHERE id=%s AND status NOT IN ('finished','stopped')
                   AND (lease_owner=%s OR lease_expires_at IS NULL OR lease_expires_at<=now())
                   RETURNING id""",
                (owner, seconds, run_id, owner),
            ).fetchone())

    def schedule_next_sync(self, run_id: str, *, seconds: int = 120, error: str | None = None) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE competition_runs SET next_sync_at=now()+(%s*interval '1 second'),
                   last_error=%s,updated_at=now() WHERE id=%s""",
                (max(1, seconds), error, run_id),
            )

    def release_expired_assignments(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            self._lock(connection)
            connection.execute(
                """UPDATE competition_wp_jobs w SET status='deferred',
                   last_error='WP attempt exceeded the 10 minute seat deadline',
                   next_attempt_at=now()+interval '5 minutes'
                   FROM competition_assignments a
                   WHERE a.run_id=%s AND a.role='wp' AND a.challenge_id=w.challenge_id
                   AND a.released_at IS NULL AND a.wp_deadline_at<=now()
                   AND w.status='running'""",
                (run_id,),
            )
            rows = connection.execute(
                """UPDATE competition_assignments a SET released_at=now(),epoch=epoch+1
                   FROM competition_challenges c
                   WHERE a.run_id=%s AND a.challenge_id=c.id AND a.released_at IS NULL
                   AND (a.lease_expires_at<=now() OR
                        (a.role<>'wp' AND c.deadline_at IS NOT NULL AND c.deadline_at<=now()) OR
                        (a.role='wp' AND a.wp_deadline_at<=now()))
                   RETURNING a.*""",
                (run_id,),
            ).fetchall()
            connection.execute(
                """UPDATE competition_challenges SET state='expired'
                   WHERE id IN (SELECT challenge_id FROM competition_run_challenges WHERE run_id=%s)
                   AND deadline_at IS NOT NULL AND deadline_at<=now() AND state NOT IN ('solved','cancelled')""",
                (run_id,),
            )
            return rows

    def recover_expired_assignments(self, run_id: str, owner: str) -> list[dict]:
        """Fence and take over solver seats left by a dead coordinator.

        Assignment leases are deliberately separate from the run lease.  A
        restarted coordinator must therefore reclaim an expired solver lease
        before the general expiry pass releases it; otherwise the persisted
        session and its five-hour deadline would be lost on every restart.
        WP leases are not reclaimed here because an expired WP attempt is
        converted to a deferred job by ``release_expired_assignments``.
        """
        if not owner:
            raise ValueError("assignment recovery owner is required")
        with self.db.connect() as connection:
            self._lock(connection)
            return connection.execute(
                """UPDATE competition_assignments a SET lease_owner=%s,
                   epoch=epoch+1,lease_expires_at=now()+interval '60 seconds'
                   FROM competition_challenges c,competition_runs r
                   WHERE a.run_id=%s AND a.released_at IS NULL
                   AND a.role<>'wp' AND a.lease_expires_at<=now()
                   AND c.id=a.challenge_id AND c.deadline_at>now()
                   AND r.id=a.run_id AND r.status='running'
                   RETURNING a.*""",
                (owner, run_id),
            ).fetchall()

    def release_run_assignments(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            self._lock(connection)
            return connection.execute(
                """UPDATE competition_assignments SET released_at=now(),epoch=epoch+1
                   WHERE run_id=%s AND released_at IS NULL RETURNING *""",
                (run_id,),
            ).fetchall()

    def heartbeat_run_assignments(self, run_id: str, owner: str) -> int:
        with self.db.connect() as connection:
            cursor = connection.execute(
                """UPDATE competition_assignments SET lease_expires_at=now()+interval '60 seconds'
                   WHERE run_id=%s AND lease_owner=%s AND released_at IS NULL""",
                (run_id, owner),
            )
            return cursor.rowcount

    def find_assignment(self, run_id: str, challenge_id: str, member: str) -> dict | None:
        with self.db.connect() as connection:
            return connection.execute(
                """SELECT * FROM competition_assignments
                   WHERE run_id=%s AND challenge_id=%s AND member=%s AND released_at IS NULL""",
                (run_id, challenge_id, member),
            ).fetchone()

    def submission(self, submission_id: str, *, reveal_candidate: bool = False) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM competition_submissions WHERE id=%s", (submission_id,),
            ).fetchone()
            if row is None:
                raise KeyError(submission_id)
            if not reveal_candidate:
                row = dict(row)
                row.pop("candidate", None)
            return row

    def authorize_message(self, message) -> bool:
        data = message.model_dump(mode="json") if hasattr(message, "model_dump") else dict(message)
        if data.get("protocol_version") != 1:
            return False
        with self.db.connect() as connection:
            sender = connection.execute(
                """SELECT 1 FROM competition_assignments
                   WHERE run_id=%s AND challenge_id=%s AND member=%s AND session_id=%s
                   AND epoch=%s AND released_at IS NULL AND lease_expires_at>now()""",
                (
                    data.get("run_id"), data.get("challenge_id"),
                    data.get("sender_member_id"), data.get("sender_session_id"),
                    data.get("lease_epoch"),
                ),
            ).fetchone()
            receiver = connection.execute(
                """SELECT 1 FROM competition_assignments
                   WHERE run_id=%s AND challenge_id=%s AND member=%s AND session_id=%s
                   AND released_at IS NULL AND lease_expires_at>now()""",
                (
                    data.get("run_id"), data.get("challenge_id"),
                    data.get("receiver_member_id"), data.get("receiver_session_id"),
                ),
            ).fetchone()
            return bool(sender and receiver)

    def claim_message(self, message) -> dict | None:
        data = message.model_dump(mode="json") if hasattr(message, "model_dump") else dict(message)
        if not self.authorize_message(data):
            raise CompetitionConflict("message session or lease is not active")
        with self.db.connect() as connection:
            self._lock(connection)
            existing = connection.execute(
                """SELECT * FROM competition_messages
                   WHERE run_id=%s AND request_id=%s FOR UPDATE""",
                (data["run_id"], data["request_id"]),
            ).fetchone()
            if existing:
                if existing["envelope"] != data:
                    raise CompetitionConflict("request id reused with a different message")
                if existing["status"] == "done":
                    return existing["result"]
                return {"status": "in_progress", "request_id": data["request_id"]}
            connection.execute(
                """INSERT INTO competition_messages
                   (run_id,challenge_id,request_id,envelope,status)
                   VALUES (%s,%s,%s,%s,'running')""",
                (data["run_id"], data["challenge_id"], data["request_id"], Jsonb(data)),
            )
            return None

    def persist_recon_message(self, message) -> dict:
        """Persist an asynchronous message before ZeroMQ announces it.

        ``competition_messages.sequence`` is the delivery order.  The caller
        publishes the returned row, never the uncommitted input envelope, so
        subscribers can always recover the same record from PostgreSQL.
        """
        data = message.model_dump(mode="json") if hasattr(message, "model_dump") else dict(message)
        if data.get("message_type") not in {"recon", "status", "context", "cancel", "result"}:
            raise CompetitionConflict("message type is not asynchronous")
        if not self.authorize_message(data):
            raise CompetitionConflict("message session or lease is not active")
        with self.db.connect() as connection:
            self._lock(connection)
            existing = connection.execute(
                """SELECT sequence,envelope,status,result,created_at
                   FROM competition_messages
                   WHERE run_id=%s AND request_id=%s FOR UPDATE""",
                (data["run_id"], data["request_id"]),
            ).fetchone()
            if existing:
                previous = dict(existing["envelope"])
                # The database sequence is authoritative.  A retry normally
                # rebuilds the envelope with its default sequence of zero.
                previous.pop("sequence", None)
                candidate = dict(data)
                candidate.pop("sequence", None)
                if previous != candidate:
                    raise CompetitionConflict("request id reused with a different message")
                if existing["status"] != "done":
                    raise CompetitionConflict("message is still owned by a synchronous execution")
                return existing
            row = connection.execute(
                """INSERT INTO competition_messages
                   (run_id,challenge_id,request_id,envelope,status,result)
                   VALUES (%s,%s,%s,%s,'done',%s)
                   RETURNING sequence,envelope,status,result,created_at""",
                (
                    data["run_id"], data["challenge_id"], data["request_id"],
                    Jsonb(data), Jsonb(data.get("payload") or {}),
                ),
            ).fetchone()
            return row

    def finish_message(self, message, result: dict) -> None:
        data = message.model_dump(mode="json") if hasattr(message, "model_dump") else dict(message)
        with self.db.connect() as connection:
            row = connection.execute(
                """UPDATE competition_messages SET status='done',result=%s
                   WHERE run_id=%s AND request_id=%s AND status='running'
                   RETURNING sequence""",
                (Jsonb(result), data["run_id"], data["request_id"]),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    """SELECT result FROM competition_messages
                       WHERE run_id=%s AND request_id=%s AND status='done'""",
                    (data["run_id"], data["request_id"]),
                ).fetchone()
                if not existing or existing["result"] != result:
                    raise CompetitionConflict("message is not owned by this execution")

    def replay_messages(
        self, session_id: str, *, after: int | None = None, limit: int = 500
    ) -> list[dict]:
        with self.db.connect() as connection:
            if after is None:
                cursor = connection.execute(
                    "SELECT sequence FROM competition_message_cursors WHERE session_id=%s",
                    (session_id,),
                ).fetchone()
                after = cursor["sequence"] if cursor else 0
            return connection.execute(
                """SELECT sequence,envelope,status,result,created_at
                   FROM competition_messages
                   WHERE envelope->>'receiver_session_id'=%s AND sequence>%s
                   ORDER BY sequence LIMIT %s""",
                (session_id, max(0, after), max(1, min(limit, 1000))),
            ).fetchall()

    def advance_message_cursor(self, session_id: str, sequence: int) -> int:
        if sequence < 0:
            raise ValueError("message cursor cannot be negative")
        with self.db.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM competition_sessions WHERE id=%s", (session_id,),
            ).fetchone():
                raise KeyError(session_id)
            maximum = connection.execute(
                """SELECT COALESCE(max(sequence),0) AS value FROM competition_messages
                   WHERE envelope->>'receiver_session_id'=%s""",
                (session_id,),
            ).fetchone()["value"]
            if sequence > maximum:
                raise CompetitionConflict("cursor cannot pass the last persisted message")
            row = connection.execute(
                """INSERT INTO competition_message_cursors (session_id,sequence)
                   VALUES (%s,%s) ON CONFLICT (session_id) DO UPDATE
                   SET sequence=GREATEST(competition_message_cursors.sequence,EXCLUDED.sequence)
                   RETURNING sequence""",
                (session_id, sequence),
            ).fetchone()
            return row["sequence"]

    def claim_wp_job(self, run_id: str, owner: str) -> dict | None:
        with self.db.connect() as connection:
            self._lock(connection)
            job = connection.execute(
                """SELECT w.* FROM competition_wp_jobs w
                   JOIN competition_runs r ON r.id=w.run_id
                   WHERE w.run_id=%s AND w.status IN ('pending','deferred')
                   AND w.next_attempt_at<=now()
                   AND NOT EXISTS (SELECT 1 FROM competition_assignments a
                     WHERE a.member=w.member AND a.released_at IS NULL)
                   AND NOT EXISTS (SELECT 1 FROM competition_assignments a
                     WHERE a.challenge_id=w.challenge_id AND a.released_at IS NULL)
                   AND r.status IN ('running','paused','draining','finished')
                   ORDER BY w.next_attempt_at,w.challenge_id LIMIT 1 FOR UPDATE OF w""",
                (run_id,),
            ).fetchone()
            if job is None:
                return None
            assignment = connection.execute(
                """INSERT INTO competition_assignments
                   (id,run_id,challenge_id,session_id,member,role,position,
                    lease_owner,lease_expires_at,wp_deadline_at)
                   VALUES (%s,%s,%s,%s,%s,'wp',1,%s,
                    now()+interval '10 minutes',now()+interval '10 minutes')
                   RETURNING *""",
                (
                    new_id(), run_id, job["challenge_id"], job["session_id"],
                    job["member"], owner,
                ),
            ).fetchone()
            connection.execute(
                """UPDATE competition_wp_jobs SET status='running',attempts=attempts+1
                   WHERE challenge_id=%s""",
                (job["challenge_id"],),
            )
            return {"job": {**job, "status": "running", "attempts": job["attempts"] + 1},
                    "assignment": assignment}

    def finish_wp_job(
        self, assignment: dict, *, artifact_path: str | None = None,
        error: str | None = None,
    ) -> dict:
        with self.db.connect() as connection:
            self._lock(connection)
            active = connection.execute(
                """SELECT * FROM competition_assignments
                   WHERE id=%s AND role='wp' AND lease_owner=%s AND epoch=%s
                   AND released_at IS NULL FOR UPDATE""",
                (assignment["id"], assignment["lease_owner"], assignment["epoch"]),
            ).fetchone()
            if active is None:
                raise CompetitionConflict("WP writer lost its assignment")
            if artifact_path:
                job = connection.execute(
                    """UPDATE competition_wp_jobs SET status='done',artifact_path=%s,last_error=NULL
                       WHERE challenge_id=%s RETURNING *""",
                    (artifact_path, assignment["challenge_id"]),
                ).fetchone()
            else:
                attempts = connection.execute(
                    "SELECT attempts FROM competition_wp_jobs WHERE challenge_id=%s",
                    (assignment["challenge_id"],),
                ).fetchone()["attempts"]
                delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
                job = connection.execute(
                    """UPDATE competition_wp_jobs SET status='deferred',last_error=%s,
                       next_attempt_at=now()+(%s*interval '1 second')
                       WHERE challenge_id=%s RETURNING *""",
                    ((error or "WP attempt failed")[:2000], delay, assignment["challenge_id"]),
                ).fetchone()
            connection.execute(
                """UPDATE competition_assignments SET released_at=now(),epoch=epoch+1
                   WHERE id=%s""",
                (assignment["id"],),
            )
            return job
