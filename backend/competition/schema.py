"""Version 1 additive storage contract. Keep these statements migration-stable."""

COMPETITION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS workflow_challenges (
        workflow_id TEXT NOT NULL REFERENCES workflows(id),
        external_id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id),
        PRIMARY KEY (workflow_id, external_id)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_runs (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(id),
        identity_key TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'preflight' CHECK (status IN
          ('preflight','importing','running','paused','blocked','draining','finished','stopped')),
        config_snapshot JSONB NOT NULL,
        revision BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_one_active
       ON competition_runs ((true)) WHERE status NOT IN ('finished','stopped')""",
    """CREATE TABLE IF NOT EXISTS competition_challenges (
        id TEXT PRIMARY KEY,
        identity_key TEXT NOT NULL,
        external_id TEXT NOT NULL,
        run_id TEXT REFERENCES competition_runs(id),
        project_id TEXT UNIQUE REFERENCES projects(id),
        state TEXT NOT NULL DEFAULT 'discovered',
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        first_assigned_at TIMESTAMPTZ,
        deadline_at TIMESTAMPTZ,
        wrong_count INTEGER NOT NULL DEFAULT 0 CHECK (wrong_count >= 0),
        next_submission_at TIMESTAMPTZ,
        instance_generation BIGINT NOT NULL DEFAULT 0,
        UNIQUE (identity_key, external_id, run_id),
        CHECK ((first_assigned_at IS NULL AND deadline_at IS NULL) OR
          (first_assigned_at IS NOT NULL AND deadline_at = first_assigned_at + interval '5 hours'))
    )""",
    """CREATE TABLE IF NOT EXISTS competition_run_challenges (
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        PRIMARY KEY (run_id, challenge_id)
    )""",
    "ALTER TABLE competition_challenges ADD COLUMN IF NOT EXISTS run_id TEXT REFERENCES competition_runs(id)",
    "ALTER TABLE competition_challenges DROP CONSTRAINT IF EXISTS competition_challenges_identity_key_external_id_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_challenge_identity_run ON competition_challenges(identity_key,external_id,run_id)",
    """CREATE TABLE IF NOT EXISTS competition_sessions (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        challenge_id TEXT REFERENCES competition_challenges(id),
        member TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'active',
        config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
        next_sequence BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (id, run_id, challenge_id, member)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_session_events (
        session_id TEXT NOT NULL REFERENCES competition_sessions(id),
        sequence BIGINT NOT NULL,
        event_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, sequence),
        UNIQUE (session_id, event_key)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_assignments (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        session_id TEXT NOT NULL,
        member TEXT NOT NULL CHECK (member IN
          ('amber','agate','topaz','sugilite','aventurine','pearl','sapphire','jade','obsidian','opal')),
        role TEXT NOT NULL CHECK (role IN ('primary','helper','wp')),
        position SMALLINT NOT NULL CHECK (position IN (1,2)),
        epoch BIGINT NOT NULL DEFAULT 1,
        lease_owner TEXT NOT NULL,
        lease_expires_at TIMESTAMPTZ NOT NULL,
        wp_deadline_at TIMESTAMPTZ,
        released_at TIMESTAMPTZ,
        FOREIGN KEY (session_id, run_id, challenge_id, member)
          REFERENCES competition_sessions(id, run_id, challenge_id, member),
        FOREIGN KEY (run_id, challenge_id)
          REFERENCES competition_run_challenges(run_id, challenge_id)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_member_slot
       ON competition_assignments(member) WHERE released_at IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_challenge_slot
       ON competition_assignments(challenge_id,position) WHERE released_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS competition_questions (
        id TEXT PRIMARY KEY,
        operation_key TEXT NOT NULL UNIQUE,
        run_id TEXT REFERENCES competition_runs(id),
        ops_session_id TEXT REFERENCES sessions(id),
        workflow_id TEXT REFERENCES workflows(id),
        secret_name TEXT,
        title TEXT NOT NULL,
        options JSONB NOT NULL DEFAULT '[]'::jsonb,
        sensitive BOOLEAN NOT NULL DEFAULT false,
        answer JSONB,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','answered','consumed','cancelled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        answered_at TIMESTAMPTZ
    )""",
    """CREATE OR REPLACE FUNCTION competition_preserve_deadline() RETURNS trigger
       LANGUAGE plpgsql AS $$ BEGIN
       IF OLD.first_assigned_at IS NOT NULL AND
          (NEW.first_assigned_at IS DISTINCT FROM OLD.first_assigned_at OR
           NEW.deadline_at IS DISTINCT FROM OLD.deadline_at) THEN
           RAISE EXCEPTION 'competition challenge deadline is immutable';
       END IF;
       RETURN NEW;
       END $$""",
    "DROP TRIGGER IF EXISTS competition_deadline_immutable ON competition_challenges",
    """CREATE TRIGGER competition_deadline_immutable BEFORE UPDATE ON competition_challenges
       FOR EACH ROW EXECUTE FUNCTION competition_preserve_deadline()""",
    "ALTER TABLE competition_questions ADD COLUMN IF NOT EXISTS ops_session_id TEXT REFERENCES sessions(id)",
    "ALTER TABLE competition_questions ADD COLUMN IF NOT EXISTS workflow_id TEXT REFERENCES workflows(id)",
    "ALTER TABLE competition_questions ADD COLUMN IF NOT EXISTS secret_name TEXT",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS last_error TEXT",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE competition_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE competition_session_events ADD COLUMN IF NOT EXISTS event_id BIGSERIAL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_competition_event_id ON competition_session_events(event_id)",
    """CREATE TABLE IF NOT EXISTS competition_run_events (
        event_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        kind TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_competition_run_events_run ON competition_run_events(run_id,event_id)",
    """CREATE TABLE IF NOT EXISTS competition_instances (
        challenge_id TEXT PRIMARY KEY REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        external_id TEXT NOT NULL,
        state TEXT NOT NULL,
        owned BOOLEAN NOT NULL DEFAULT true,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        next_renew_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS competition_submissions (
        id TEXT PRIMARY KEY,
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        session_id TEXT NOT NULL REFERENCES competition_sessions(id),
        member TEXT NOT NULL,
        candidate TEXT NOT NULL,
        candidate_hash TEXT NOT NULL,
        evidence TEXT NOT NULL,
        instance_generation BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        platform_submission_id TEXT,
        verdict JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        submitted_at TIMESTAMPTZ,
        UNIQUE (challenge_id,candidate_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_wp_jobs (
        challenge_id TEXT PRIMARY KEY REFERENCES competition_challenges(id),
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        member TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES competition_sessions(id),
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_error TEXT,
        artifact_path TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS competition_messages (
        sequence BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id),
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        request_id TEXT NOT NULL,
        envelope JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id,request_id)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_message_cursors (
        session_id TEXT PRIMARY KEY REFERENCES competition_sessions(id),
        sequence BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS competition_artifacts (
        challenge_id TEXT NOT NULL REFERENCES competition_challenges(id),
        path TEXT NOT NULL,
        version BIGINT NOT NULL DEFAULT 1,
        sha256 TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        updated_by_session TEXT NOT NULL REFERENCES competition_sessions(id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (challenge_id,path)
    )""",
    """CREATE TABLE IF NOT EXISTS competition_run_observations (
        observation_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES competition_runs(id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        sampled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    "CREATE INDEX IF NOT EXISTS ix_competition_run_observations_run ON competition_run_observations(run_id,observation_id)",
)
