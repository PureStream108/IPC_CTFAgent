"""Complete the competition runtime, recovery, verdict and replay schema."""

from alembic import op


revision = "20260920_0004"
down_revision = "20260920_0003"
branch_labels = None
depends_on = None


STATEMENTS = (
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS last_error TEXT",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE competition_session_events ADD COLUMN IF NOT EXISTS event_id BIGSERIAL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_event_id ON competition_session_events(event_id)",
    """CREATE TABLE IF NOT EXISTS competition_run_events (
        event_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        kind TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_competition_run_events_run ON competition_run_events(run_id,event_id)",
    """CREATE TABLE IF NOT EXISTS competition_instances (
        challenge_id TEXT PRIMARY KEY REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        external_id TEXT NOT NULL,
        state TEXT NOT NULL,
        owned BOOLEAN NOT NULL DEFAULT true,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        next_renew_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS competition_submissions (
        id TEXT PRIMARY KEY,
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        session_id TEXT NOT NULL REFERENCES competition_sessions(id),
        member TEXT NOT NULL,
        candidate TEXT NOT NULL,
        candidate_hash TEXT NOT NULL,
        evidence TEXT NOT NULL,
        instance_generation BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        platform_submission_id TEXT,
        verdict JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        submitted_at TIMESTAMPTZ,
        UNIQUE (challenge_id,candidate_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_wp_jobs (
        challenge_id TEXT PRIMARY KEY REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        member TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES competition_sessions(id),
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_error TEXT,
        artifact_path TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS competition_messages (
        sequence BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        request_id TEXT NOT NULL,
        envelope JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id,request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_message_cursors (
        session_id TEXT PRIMARY KEY REFERENCES competition_sessions(id),
        sequence BIGINT NOT NULL DEFAULT 0
    )""",
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError(
        "Competition runtime history cannot be downgraded without an explicit data export."
    )
