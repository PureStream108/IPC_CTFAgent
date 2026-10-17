
CONTROL_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS competition_control_operations (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        action TEXT NOT NULL CHECK(action IN ('pause','resume','stop','refresh')),
        requested_revision BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed')),
        owner TEXT,
        epoch BIGINT NOT NULL DEFAULT 0,
        lease_expires_at TIMESTAMPTZ,
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )""",
    """CREATE INDEX IF NOT EXISTS ix_competition_control_pending
       ON competition_control_operations(created_at) WHERE status IN ('queued','running')""",
)
