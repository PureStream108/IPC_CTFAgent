"""Unified agent sessions: append-only transcript shared by every runtime.

The competition tables are the only existing producer of durable agent
history, so they are backfilled here.  Session ids are preserved verbatim:
five tables reference ``competition_sessions(id)``, and reusing the id lets
those foreign keys be re-pointed at ``agent_sessions`` without rewriting any
child row.
"""

from alembic import op

from backend.agent.schema import AGENT_SCHEMA


revision = "20260929_0009"
down_revision = "20260925_0008"
branch_labels = None
depends_on = None


# competition_assignments pins a session to one member through a composite
# foreign key.  Member handoff - keeping the transcript while a different
# Member takes the seat - is unrepresentable until it becomes a single-column
# reference to agent_sessions.
#
# The old constraint names are discovered from the catalog rather than spelled
# out: PostgreSQL truncates generated names to 63 characters, and the
# composite key on competition_assignments is well past that.
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

    # 1) Backfill sessions, keeping ids so child foreign keys stay valid.
    op.execute(
        """
        INSERT INTO agent_sessions
            (id, kind, task_key, model_snapshot, state, next_sequence,
             checkpoint, created_at, updated_at)
        SELECT s.id, 'competition',
               'competition:' || s.run_id || ':' || COALESCE(s.challenge_id, '-'),
               s.config_snapshot,
               CASE WHEN s.state = 'active' THEN 'active' ELSE 'finished' END,
               s.next_sequence, s.checkpoint, s.created_at, s.created_at
        FROM competition_sessions s
        ON CONFLICT (id) DO NOTHING
        """
    )
    # A historical database can hold several active sessions for one challenge
    # (one per member).  uq_agent_session_active_task allows exactly one live
    # root session per task, so retire all but the newest instead of failing
    # the migration.
    op.execute(
        """
        UPDATE agent_sessions SET state='superseded'
        WHERE parent_session_id IS NULL AND state='active' AND id NOT IN (
            SELECT DISTINCT ON (task_key) id FROM agent_sessions
            WHERE parent_session_id IS NULL AND state='active'
            ORDER BY task_key, created_at DESC, id DESC
        )
        """
    )

    # 2) Copy the transcript verbatim.  Legacy compaction events additionally
    #    become agent_compactions rows so the projection logic has one source,
    #    but the original events are preserved untouched.
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

    # 3) Re-point child foreign keys at agent_sessions.  competition_sessions
    #    survives read-only for rollback inspection.
    for table, new_constraint, column in _REPOINTED_FOREIGN_KEYS:
        op.execute(_drop_constraints_referencing_competition_sessions(table))
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {new_constraint}"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {new_constraint} "
            f"FOREIGN KEY ({column}) REFERENCES agent_sessions(id)"
        )

    # 4) Give every backfilled assignment a writer row so the existing lease
    #    keeps fencing writes through the new contract.
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

    # 5) The Claude Code sidecar is gone: Ops conversations now reference an
    #    agent session instead of an external provider session id.
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS claude_session_id")


def downgrade():
    raise RuntimeError(
        "Unified agent sessions cannot be downgraded: child foreign keys were "
        "re-pointed and the event history was copied. Restore from a backup."
    )
