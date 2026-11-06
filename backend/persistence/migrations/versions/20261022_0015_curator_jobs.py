"""Sugilite curator metadata on background jobs."""

from alembic import op

revision = "20261022_0015"
down_revision = "20261021_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS project_id TEXT"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS member TEXT"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS intent_id TEXT"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS digest_state TEXT NOT NULL DEFAULT 'none'"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS digest_json JSONB"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS digest_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS digest_attempts INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_curator "
        "ON agent_jobs(project_id,digest_state,finished_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_member_digest "
        "ON agent_jobs(project_id,member,digest_state,finished_at)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_agent_jobs_member_digest")
    op.execute("DROP INDEX IF EXISTS ix_agent_jobs_curator")
    op.drop_column("agent_jobs", "digest_attempts")
    op.drop_column("agent_jobs", "delivered_at")
    op.drop_column("agent_jobs", "digest_at")
    op.drop_column("agent_jobs", "digest_json")
    op.drop_column("agent_jobs", "digest_state")
    op.drop_column("agent_jobs", "intent_id")
    op.drop_column("agent_jobs", "member")
    op.drop_column("agent_jobs", "project_id")
