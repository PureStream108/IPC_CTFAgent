"""Remove provider credentials accidentally persisted in early run snapshots."""

from alembic import op


revision = "20260921_0006"
down_revision = "20260920_0005"
branch_labels = None
depends_on = None


def upgrade():
    # JSONB ``-`` is idempotent and also handles snapshots created before the
    # runtime began recording an explicit ``member_config_source`` marker.
    op.execute(
        """UPDATE competition_runs
           SET config_snapshot = jsonb_set(
               config_snapshot,
               '{member_config}',
               COALESCE(config_snapshot->'member_config', '{}'::jsonb) - 'api_key',
               true)
           WHERE config_snapshot ? 'member_config'
             AND jsonb_typeof(config_snapshot->'member_config') = 'object'"""
    )


def downgrade():
    raise RuntimeError("Credential redaction is intentionally irreversible.")
