"""Fence competition challenge execution state to a single run."""

from alembic import op


revision = "20260925_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE competition_challenges ADD COLUMN IF NOT EXISTS "
        "run_id TEXT REFERENCES competition_runs(id)"
    )
    op.execute(
        "ALTER TABLE competition_challenges DROP CONSTRAINT IF EXISTS "
        "competition_challenges_identity_key_external_id_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_challenge_identity_run "
        "ON competition_challenges(identity_key, external_id, run_id)"
    )


def downgrade():
    raise RuntimeError("Run-scoped challenge state cannot be downgraded without an export.")
