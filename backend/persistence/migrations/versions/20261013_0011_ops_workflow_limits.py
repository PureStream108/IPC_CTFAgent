"""Bind one Ops conversation to one workflow and add run limit overrides."""

from alembic import op

revision = "20261013_0011"
down_revision = "20261008_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_workflow_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_sessions_workflow ON sessions(agent_workflow_id)"
    )
    op.execute(
        "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS limits JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        """UPDATE workflows w SET name='Imported competition ' || w.id
           WHERE lower(trim(w.name)) IN ('proposed','probe','workflow','new workflow')
             AND EXISTS (SELECT 1 FROM competition_runs r WHERE r.workflow_id=w.id)"""
    )
    op.execute(
        """DELETE FROM workflows w
           WHERE lower(trim(w.name)) IN ('proposed','probe','workflow','new workflow')
             AND NOT EXISTS (SELECT 1 FROM competition_runs r WHERE r.workflow_id=w.id)
             AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.agent_workflow_id=w.id)"""
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_ops_sessions_workflow")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS agent_workflow_id")
    op.execute("ALTER TABLE competition_runs DROP COLUMN IF EXISTS limits")
