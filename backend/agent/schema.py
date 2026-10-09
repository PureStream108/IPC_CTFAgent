"""Storage contract for the unified agent runtime. Keep migration-stable.

The central invariant: ``agent_events`` is append-only. Nothing in this
contract modifies or deletes an event, so the evidence chain of a task
survives compaction, member handoff, and worker restarts. Context
reconstruction is a projection over these events (see
``backend.agent.context``), and a compaction is a *derived* summary recorded
in ``agent_compactions`` rather than a destructive rewrite of the transcript.
"""

AGENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS agent_sessions (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('ops','competition','project')),
        task_key TEXT NOT NULL,
        parent_session_id TEXT REFERENCES agent_sessions(id),
        lineage_reason TEXT CHECK (lineage_reason IS NULL OR lineage_reason IN
          ('fork','resume','redirect','member_handoff','model_switch','helper')),
        provider_session_id TEXT,
        provider_session_state TEXT NOT NULL DEFAULT 'unknown'
          CHECK (provider_session_state IN ('unknown','native','degraded','unsupported')),
        model_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        state TEXT NOT NULL DEFAULT 'active'
          CHECK (state IN ('active','idle','finished','failed','superseded')),
        next_sequence BIGINT NOT NULL DEFAULT 1,
        token_budget JSONB NOT NULL DEFAULT '{}'::jsonb,
        checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # One live root session per task. Helper seats attach as child sessions
    # (parent_session_id IS NOT NULL) so they never contend for this slot.
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_session_active_task
       ON agent_sessions(task_key)
       WHERE state='active' AND parent_session_id IS NULL""",
    "CREATE INDEX IF NOT EXISTS ix_agent_sessions_task ON agent_sessions(task_key,created_at)",
    # "Who may write" is deliberately decoupled from member identity: a
    # handoff replaces the owner of an existing session instead of starting a
    # new transcript.
    """CREATE TABLE IF NOT EXISTS agent_session_writers (
        session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id) ON DELETE CASCADE,
        worker_kind TEXT NOT NULL,
        owner TEXT NOT NULL,
        epoch BIGINT NOT NULL DEFAULT 1,
        lease_expires_at TIMESTAMPTZ NOT NULL,
        last_renewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS agent_turns (
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL,
        owner TEXT NOT NULL,
        epoch BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK (status IN
          ('running','completed','cancelled','failed','truncated')),
        prompt_tokens INTEGER,
        output_tokens INTEGER,
        reasoning_tokens INTEGER,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        PRIMARY KEY (session_id, turn)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_events (
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        sequence BIGINT NOT NULL,
        event_id BIGSERIAL,
        turn INTEGER,
        event_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN
          ('user_message','provider_messages','reasoning','tool_call','tool_result',
           'status','error','handoff','loop_detected',
           -- Transitional: the competition runtime still records compaction as
           -- an event. Phase 2 moves it to agent_compactions and these two
           -- kinds leave the contract.
           'context_compacted','compression_failed')),
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, sequence),
        UNIQUE (session_id, event_key)
    )""",
    # event_id is globally monotonic and drives SSE cursors across sessions.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_event_id ON agent_events(event_id)",
    """CREATE TABLE IF NOT EXISTS agent_compactions (
        compaction_id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        up_to_sequence BIGINT NOT NULL,
        summary JSONB NOT NULL,
        token_estimate INTEGER,
        generated_by JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS ix_agent_compactions_session
       ON agent_compactions(session_id, up_to_sequence DESC)""",
    # Long-running sandbox work. The 60s exec ceiling made brute force, large
    # file analysis and slow scans impossible, so a job outlives the turn that
    # started it and is polled instead of awaited.
    #
    # The handle is deliberately the PID file plus the output files in the
    # workspace volume, not a docker exec id: an exec id dies with a container
    # restart while the volume survives, and a job whose fate is unknown must
    # be reported as 'lost' rather than guessed to have exited 0.
    """CREATE TABLE IF NOT EXISTS agent_jobs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        command TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running'
          CHECK (state IN ('running','exited','cancelled','lost')),
        exit_code INTEGER,
        stdout_artifact TEXT,
        stderr_artifact TEXT,
        workdir TEXT,
        pid INTEGER,
        stdout_offset BIGINT NOT NULL DEFAULT 0,
        stderr_offset BIGINT NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_progress_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )""",
    """CREATE INDEX IF NOT EXISTS ix_agent_jobs_session
       ON agent_jobs(session_id, started_at DESC)""",
    """CREATE INDEX IF NOT EXISTS ix_agent_jobs_running
       ON agent_jobs(session_id) WHERE state='running'""",
    # "Is this session actually progressing" is four different questions that
    # the UI used to render as one green dot: the API is up, the coordinator
    # ticks, a worker holds the seat, and the task advances. Each column below
    # answers exactly one of them, so a stall can be told apart from slow work.
    #
    # Deliberately a view, not a table: every input is already durable, and a
    # cached copy would be one more thing to invalidate - stale health data is
    # worse than none.
    # Dropped first because CREATE OR REPLACE VIEW cannot rename, reorder or
    # retype columns: replacing it in place fails once the column list changes.
    # Nothing holds a durable reference to this view, so a drop is free.
    "DROP VIEW IF EXISTS agent_session_health",
    """CREATE OR REPLACE VIEW agent_session_health AS
       SELECT s.id AS session_id,
              s.kind,
              s.task_key,
              s.state,
              w.owner,
              w.epoch,
              w.worker_kind,
              w.last_renewed_at,
              -- The worker's own proof of life, which is what separates "the
              -- coordinator is ticking" from "a worker is turning".
              (w.lease_expires_at > now()) AS worker_live,
              w.lease_expires_at AS worker_lease_expires_at,
              e.last_event_at,
              e.last_event_kind,
              -- Counts are coalesced to 0 rather than left NULL by the outer
              -- join: "no events yet" is a number, and leaving it NULL pushes
              -- the same COALESCE into every consumer.
              COALESCE(e.event_count, 0) AS event_count,
              t.current_turn,
              t.current_turn_started_at,
              t.last_finished_turn_at,
              t.turn_status,
              COALESCE(j.running_jobs, 0) AS running_jobs,
              j.last_job_progress_at,
              -- Quiet time is measured from the most recent sign of progress of
              -- any kind. A long provider turn and a long tool both count as
              -- progress; only genuine silence accumulates here.
              GREATEST(
                COALESCE(e.last_event_at, s.created_at),
                COALESCE(t.current_turn_started_at, s.created_at),
                COALESCE(t.last_finished_turn_at, s.created_at),
                COALESCE(j.last_job_progress_at, s.created_at),
                COALESCE(w.last_renewed_at, s.created_at)
              ) AS last_progress_at,
              EXTRACT(EPOCH FROM now() - GREATEST(
                COALESCE(e.last_event_at, s.created_at),
                COALESCE(t.current_turn_started_at, s.created_at),
                COALESCE(t.last_finished_turn_at, s.created_at),
                COALESCE(j.last_job_progress_at, s.created_at),
                COALESCE(w.last_renewed_at, s.created_at)
              ))::bigint AS quiet_seconds
       FROM agent_sessions s
       LEFT JOIN agent_session_writers w ON w.session_id = s.id
       LEFT JOIN (
         SELECT session_id,
                max(created_at) AS last_event_at,
                count(*) AS event_count,
                (array_agg(kind ORDER BY sequence DESC))[1] AS last_event_kind
         FROM agent_events GROUP BY session_id
       ) e ON e.session_id = s.id
       LEFT JOIN (
         SELECT session_id,
                max(turn) AS current_turn,
                max(started_at) FILTER (WHERE status='running') AS current_turn_started_at,
                max(finished_at) AS last_finished_turn_at,
                (array_agg(status ORDER BY turn DESC))[1] AS turn_status
         FROM agent_turns GROUP BY session_id
       ) t ON t.session_id = s.id
       LEFT JOIN (
         SELECT session_id,
                count(*) FILTER (WHERE state='running') AS running_jobs,
                max(last_progress_at) AS last_job_progress_at
         FROM agent_jobs GROUP BY session_id
       ) j ON j.session_id = s.id""",
    # Ops conversations point at their agent session. The column lives on
    # ``sessions``, which is created earlier, so the reference is added here
    # once the target table exists.
    "ALTER TABLE IF EXISTS sessions ADD COLUMN IF NOT EXISTS agent_session_id TEXT",
    """DO $$ BEGIN
       IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='sessions_agent_session_fk') THEN
           ALTER TABLE sessions ADD CONSTRAINT sessions_agent_session_fk
             FOREIGN KEY (agent_session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL;
       END IF;
       END $$""",
)
