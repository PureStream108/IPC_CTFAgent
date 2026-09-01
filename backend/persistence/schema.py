from __future__ import annotations


# These additive patches run before the create statements.  On a fresh
# database ``ALTER TABLE IF EXISTS`` is a no-op; on an existing PostgreSQL
# deployment it fills in columns introduced after the first persistent build
# before indexes or runtime queries can reference them.
SCHEMA_COMPATIBILITY_STATEMENTS = (
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS external_id TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'misc'",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'created'",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS postprocess_status TEXT NOT NULL DEFAULT 'not_started'",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS terminal_reason TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS flag TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS flag_verified_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS wp_path TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS log_filename TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS runtime_phase TEXT NOT NULL DEFAULT 'idle'",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS runtime_error TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS reason_worker TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS reason_trigger TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS reason_started_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS reason_last_heartbeat_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS lease_token TEXT",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS lease_version BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS worker TEXT",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS concluded_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS lease_token TEXT",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS lease_version BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS intents ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS reports ADD COLUMN IF NOT EXISTS node_id TEXT",
    "ALTER TABLE IF EXISTS reports ADD COLUMN IF NOT EXISTS steps_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE IF EXISTS reports ADD COLUMN IF NOT EXISTS directions_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE IF EXISTS reports ADD COLUMN IF NOT EXISTS knowledge_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE IF EXISTS memories ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'diamond'",
    "ALTER TABLE IF EXISTS sessions ADD COLUMN IF NOT EXISTS claude_session_id TEXT",
    "ALTER TABLE IF EXISTS runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE IF EXISTS runs ADD COLUMN IF NOT EXISTS response_json JSONB",
    "ALTER TABLE IF EXISTS runs ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE IF EXISTS runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ",
    "ALTER TABLE IF EXISTS workflows ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft'",
    "ALTER TABLE IF EXISTS workflows ADD COLUMN IF NOT EXISTS confirmed_digest TEXT",
    "ALTER TABLE IF EXISTS workflows ADD COLUMN IF NOT EXISTS capability_hash TEXT",
)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        intent_timeout INTEGER NOT NULL DEFAULT 30,
        reason_timeout INTEGER NOT NULL DEFAULT 30
    )
    """,
    "INSERT INTO settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        external_id TEXT,
        platform TEXT,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'misc',
        status TEXT NOT NULL DEFAULT 'created',
        postprocess_status TEXT NOT NULL DEFAULT 'not_started',
        terminal_reason TEXT,
        flag TEXT,
        flag_verified_at TIMESTAMPTZ,
        wp_path TEXT,
        log_filename TEXT,
        runtime_phase TEXT NOT NULL DEFAULT 'idle',
        runtime_error TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        reason_worker TEXT,
        reason_trigger TEXT,
        reason_started_at TIMESTAMPTZ,
        reason_last_heartbeat_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_token TEXT,
        lease_version BIGINT NOT NULL DEFAULT 0,
        lease_expires_at TIMESTAMPTZ,
        last_heartbeat_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_projects_lease ON projects(lease_expires_at) WHERE lease_owner IS NOT NULL",
    # Existing databases created before the platform column keep working:
    # configure() runs these statements on every startup, so the idempotent
    # ALTER upgrades them in place.
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS platform TEXT",
    """
    CREATE TABLE IF NOT EXISTS facts (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        description TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intents (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        to_fact_id TEXT,
        description TEXT NOT NULL,
        creator TEXT NOT NULL,
        worker TEXT,
        last_heartbeat_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        concluded_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_token TEXT,
        lease_version BIGINT NOT NULL DEFAULT 0,
        lease_expires_at TIMESTAMPTZ,
        retry_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (id, project_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intents_claim ON intents(project_id, concluded_at, lease_expires_at, created_at)",
    # Goal completion is serialized through the project row lock.  Do not add
    # an unconditional runtime unique index here: an imported legacy database
    # may contain duplicate historical goal rows and must be audited instead of
    # failing every application bootstrap or silently deleting evidence.
    """
    CREATE TABLE IF NOT EXISTS intent_sources (
        intent_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        PRIMARY KEY (intent_id, project_id, fact_id),
        FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hints (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        creator TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'idle',
        start_fact_id TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (project_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_links (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        src TEXT NOT NULL,
        dst TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        member TEXT NOT NULL,
        node_id TEXT,
        progress TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        steps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        directions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        knowledge_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broadcasts (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        flag TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)",
    "INSERT INTO counters (name, value) VALUES ('project', 0) ON CONFLICT (name) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS scoped_counters (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        value BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        tags JSONB NOT NULL DEFAULT '[]'::jsonb,
        project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
        source TEXT NOT NULL DEFAULT 'diamond',
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS mem_counter (name TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)",
    "INSERT INTO mem_counter (name, value) VALUES ('memory', 0) ON CONFLICT (name) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        claude_session_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        response_json JSONB,
        error TEXT,
        started_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_runs_session ON runs(session_id, started_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_runs_one_active ON runs(session_id) WHERE status = 'running'",
    """
    CREATE TABLE IF NOT EXISTS events (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_events_session ON events(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_ops_events_run ON events(run_id, id)",
    """
    CREATE TABLE IF NOT EXISTS session_projects (
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (session_id, project_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY,
        session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        source TEXT NOT NULL,
        name TEXT NOT NULL,
        spec_json JSONB NOT NULL,
        spec_digest TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        confirmed_digest TEXT,
        capability_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_sessions (
        session_hash BYTEA PRIMARY KEY,
        expires_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS flag_submissions (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        candidate TEXT NOT NULL,
        normalized_flag TEXT NOT NULL,
        status TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_artifact TEXT,
        error TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL,
        verified_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS postprocess_jobs (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_error TEXT,
        locked_by TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_postprocess_claim ON postprocess_jobs(status, next_attempt_at, lease_expires_at)",
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
        run_id TEXT,
        kind TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_runs (
        id BIGSERIAL PRIMARY KEY,
        source_manifest JSONB NOT NULL,
        status TEXT NOT NULL,
        imported_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
        conflict_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """,
)


# One ordered contract is consumed by both runtime bootstrap and Alembic.
SCHEMA_CONTRACT_STATEMENTS = (*SCHEMA_COMPATIBILITY_STATEMENTS, *SCHEMA_STATEMENTS)
