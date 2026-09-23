"""Version shared competition artifacts to prevent silent lost updates."""

from alembic import op


revision = "20260920_0005"
down_revision = "20260920_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE IF NOT EXISTS competition_artifacts (
            challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
            path TEXT NOT NULL,
            version BIGINT NOT NULL DEFAULT 1,
            sha256 TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            updated_by_session TEXT NOT NULL REFERENCES competition_sessions(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (challenge_id,path)
        )"""
    )


def downgrade():
    raise RuntimeError(
        "Competition artifacts cannot be downgraded without an explicit data export."
    )
