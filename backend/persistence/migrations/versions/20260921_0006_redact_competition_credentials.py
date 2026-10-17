
from alembic import op

revision = "20260921_0006"
down_revision = "20260920_0005"
branch_labels = None
depends_on = None


def upgrade():
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
