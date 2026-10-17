from alembic import op

from backend.agent.schema import AGENT_SCHEMA

revision = "20260929_0009"
down_revision = "20260925_0008"
branch_labels = None
depends_on = None


_REPOINTED_FOREIGN_KEYS = (
    ("competition_assignments", "competition_assignments_session_fk", "session_id"),
    ("competition_submissions", "competition_submissions_session_fk", "session_id"),
    ("competition_wp_jobs", "competition_wp_jobs_session_fk", "session_id"),
    (
        "competition_message_cursors",
        "competition_message_cursors_session_fk",
        "session_id",
    ),
    (
        "competition_artifacts",
        "competition_artifacts_session_fk",
        "updated_by_session",
    ),
)


def _drop_constraints_referencing_competition_sessions(table: str) -> str:
    """Drop every FK from ``table`` into competition_sessions, by catalog name."""
    return f"""
        DO $$
        DECLARE target text;
        BEGIN
            FOR target IN
                SELECT c.conname FROM pg_constraint c
                WHERE c.conrelid = '{table}'::regclass
                  AND c.contype = 'f'
                  AND c.confrelid = 'competition_sessions'::regclass
            LOOP
                EXECUTE format(
                    'ALTER TABLE {table} DROP CONSTRAINT %I', target);
            END LOOP;
        END $$
    """


def upgrade():
    for statement in AGENT_SCHEMA:
        op.execute(statement)

    op.execute(
        """
        CREATE TEMP TABLE _agent_session_state AS
        WITH ranked AS (
            SELECT s.id,
                   'competition:' || s.run_id || ':' || COALESCE(s.challenge_id, '-')
                       AS task_key,
                   s.state = 'active' AS was_active,
                   EXISTS (
                       SELECT 1 FROM competition_assignments a
                       WHERE a.session_id = s.id
                         AND a.released_at IS NULL
                         AND a.lease_expires_at > now()
                   ) AS live_seat
            FROM competition_sessions s
        ),
        keep AS (
            SELECT DISTINCT ON (task_key) id FROM ranked
            WHERE was_active
            ORDER BY task_key, live_seat DESC, id
        )
        SELECT r.id, r.task_key, r.live_seat,
               CASE
                   WHEN NOT r.was_active THEN 'finished'
                   WHEN r.id IN (SELECT id FROM keep) THEN 'active'
                   ELSE 'superseded'
               END AS state
        FROM ranked r
        """
    )

    op.execute(
        """
        INSERT INTO agent_sessions
            (id, kind, task_key, model_snapshot, state, next_sequence,
             checkpoint, created_at, updated_at)
        SELECT s.id, 'competition', t.task_key, s.config_snapshot, t.state,
               s.next_sequence, s.checkpoint, s.created_at, s.created_at
        FROM competition_sessions s
        JOIN _agent_session_state t ON t.id = s.id
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO agent_events
            (session_id, sequence, turn, event_key, kind, payload, created_at)
        SELECT e.session_id, e.sequence, NULL, e.event_key,
               CASE
                   WHEN e.kind IN ('user_message','provider_messages','reasoning',
                                   'tool_call','tool_result','status','error',
                                   'handoff','loop_detected','context_compacted',
                                   'compression_failed') THEN e.kind
                   ELSE 'status'
               END,
               CASE
                   WHEN e.kind IN ('user_message','provider_messages','reasoning',
                                   'tool_call','tool_result','status','error',
                                   'handoff','loop_detected','context_compacted',
                                   'compression_failed') THEN e.payload
                   ELSE jsonb_build_object('legacy_kind', e.kind, 'payload', e.payload)
               END,
               e.created_at
        FROM competition_session_events e
        JOIN agent_sessions s ON s.id = e.session_id
        ON CONFLICT (session_id, sequence) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO agent_compactions
            (session_id, up_to_sequence, summary, generated_by, created_at)
        SELECT e.session_id, e.sequence,
               jsonb_build_object(
                   'legacy', true,
                   'verified_facts', '[]'::jsonb,
                   'failed_paths', '[]'::jsonb,
                   'open_questions', '[]'::jsonb,
                   'artifacts', '[]'::jsonb,
                   'key_offsets', '[]'::jsonb,
                   'next_hypotheses', '[]'::jsonb,
                   'text', COALESCE(e.payload->>'summary', ''),
                   'messages', COALESCE(e.payload->'messages', '[]'::jsonb)
               ),
               jsonb_build_object('source', 'migration:20260929_0009'),
               e.created_at
        FROM competition_session_events e
        JOIN agent_sessions s ON s.id = e.session_id
        WHERE e.kind = 'context_compacted'
        """
    )
    op.execute(
        """
        UPDATE agent_sessions s SET next_sequence = GREATEST(
            s.next_sequence,
            COALESCE((SELECT MAX(e.sequence) + 1 FROM agent_events e
                      WHERE e.session_id = s.id), 1))
        """
    )
    op.execute(
        """
        INSERT INTO agent_events
            (session_id, sequence, event_key, kind, payload)
        SELECT t.id, s.next_sequence, 'migration:20260929_0009:superseded', 'status',
               jsonb_build_object(
                   'status', 'superseded_by_migration',
                   'reason', 'one active root session per task is now enforced',
                   'task_key', t.task_key,
                   'had_live_assignment', t.live_seat,
                   'migration', '20260929_0009')
        FROM _agent_session_state t
        JOIN agent_sessions s ON s.id = t.id
        WHERE t.state = 'superseded'
        ON CONFLICT (session_id, sequence) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE agent_sessions SET next_sequence = next_sequence + 1
        WHERE id IN (
            SELECT id FROM _agent_session_state WHERE state = 'superseded'
        )
        """
    )

    for table, new_constraint, column in _REPOINTED_FOREIGN_KEYS:
        op.execute(_drop_constraints_referencing_competition_sessions(table))
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {new_constraint}"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {new_constraint} "
            f"FOREIGN KEY ({column}) REFERENCES agent_sessions(id)"
        )

    op.execute(
        """
        INSERT INTO agent_session_writers
            (session_id, worker_kind, owner, epoch, lease_expires_at, last_renewed_at)
        SELECT DISTINCT ON (a.session_id) a.session_id, 'competition-assignment',
               a.lease_owner, a.epoch, a.lease_expires_at, now()
        FROM competition_assignments a
        JOIN agent_sessions s ON s.id = a.session_id
        WHERE a.released_at IS NULL
        ORDER BY a.session_id, a.epoch DESC
        ON CONFLICT (session_id) DO NOTHING
        """
    )

    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS claude_session_id")


def downgrade():
    raise RuntimeError(
        "Unified agent sessions cannot be downgraded: child foreign keys were "
        "re-pointed and the event history was copied. Restore from a backup."
    )
