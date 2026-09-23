"""Fence legacy/competition schedulers and persist run observations."""

from alembic import op


revision = "20260923_0007"
down_revision = "20260921_0006"
branch_labels = None
depends_on = None


STATEMENTS = (
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS engine_kind TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS engine_owner TEXT",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS engine_epoch BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS engine_lease_expires_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_projects_engine_lease ON projects(engine_lease_expires_at) WHERE engine_owner IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS competition_run_observations (
        observation_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        sampled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    "CREATE INDEX IF NOT EXISTS ix_competition_run_observations_run ON competition_run_observations(run_id,observation_id)",
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Engine fencing and observations are intentionally irreversible.")
