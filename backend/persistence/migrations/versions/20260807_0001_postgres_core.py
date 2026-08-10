"""create PostgreSQL-only IPC schema"""

from alembic import op

from backend.persistence.schema import SCHEMA_CONTRACT_STATEMENTS

revision = "20260807_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in SCHEMA_CONTRACT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "migration_runs",
        "audit_events",
        "postprocess_jobs",
        "flag_submissions",
        "active_sessions",
        "workflows",
        "session_projects",
        "events",
        "runs",
        "messages",
        "sessions",
        "mem_counter",
        "memories",
        "scoped_counters",
        "counters",
        "broadcasts",
        "attachments",
        "reports",
        "agent_links",
        "agents",
        "hints",
        "intent_sources",
        "intents",
        "facts",
        "projects",
        "settings",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
