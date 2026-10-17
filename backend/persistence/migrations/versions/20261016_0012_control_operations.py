from alembic import op

from backend.agent.schema import AGENT_SCHEMA
from backend.competition.control_schema import CONTROL_SCHEMA

revision = "20261016_0012"
down_revision = "20261013_0011"
branch_labels = None
depends_on = None


def upgrade():
    for statement in CONTROL_SCHEMA:
        op.execute(statement)
    for statement in AGENT_SCHEMA:
        if statement.startswith("CREATE OR REPLACE VIEW agent_session_health"):
            op.execute(statement)


def downgrade():
    op.execute("DROP TABLE IF EXISTS competition_control_operations")
