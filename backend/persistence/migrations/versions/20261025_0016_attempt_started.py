"""Anchor the soft deadline to the current attempt.

first_assigned_at is written once (and the platform deadline trigger makes it
immutable), so a challenge that came back from a park was still measured from
its very first assignment: the scheduler parked it again on the next tick and
the retry only ever got about a second of work.
"""

from alembic import op

revision = "20261025_0016"
down_revision = "20261022_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE competition_challenges ADD COLUMN IF NOT EXISTS attempt_started_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE competition_challenges SET attempt_started_at = first_assigned_at "
        "WHERE attempt_started_at IS NULL AND first_assigned_at IS NOT NULL"
    )


def downgrade():
    op.execute("ALTER TABLE competition_challenges DROP COLUMN IF EXISTS attempt_started_at")
