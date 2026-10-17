from alembic import op

revision = "20261008_0010"
down_revision = "20260929_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            command TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'running'
              CHECK (state IN ('running','exited','cancelled','lost')),
            exit_code INTEGER,
            stdout_artifact TEXT,
            stderr_artifact TEXT,
            workdir TEXT,
            pid INTEGER,
            stdout_offset BIGINT NOT NULL DEFAULT 0,
            stderr_offset BIGINT NOT NULL DEFAULT 0,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_progress_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_session "
        "ON agent_jobs(session_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_running "
        "ON agent_jobs(session_id) WHERE state='running'"
    )
    op.execute("DROP VIEW IF EXISTS agent_session_health")
    op.execute(_health_view_statement())


def _health_view_statement() -> str:
    from backend.agent.schema import AGENT_SCHEMA

    for statement in AGENT_SCHEMA:
        if "CREATE OR REPLACE VIEW agent_session_health" in statement:
            return statement
    raise RuntimeError("agent_session_health is missing from AGENT_SCHEMA")


def downgrade():
    op.execute("DROP VIEW IF EXISTS agent_session_health")
    op.execute("DROP TABLE IF EXISTS agent_jobs")
