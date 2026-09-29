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
