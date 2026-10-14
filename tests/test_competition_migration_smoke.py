from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from psycopg import connect
from psycopg.sql import SQL, Identifier

pytestmark = pytest.mark.postgres


def _dsn_for_database(base: str, database: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.netloc:
        raise ValueError("IPC_TEST_DATABASE_URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))


def _head_revision(config: Config) -> str:
    """The single head of the migration graph.

    Asserting against this rather than a literal id keeps the test about "an
    old database reaches head" instead of about which revision is current, and
    fails loudly if the graph ever grows a second head.
    """
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"migration graph has multiple heads: {heads}"
    return heads[0]


def test_old_revision_upgrades_forward_and_keeps_legacy_rows(monkeypatch):
    base = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("set IPC_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database_name = f"ipc_upgrade_{uuid.uuid4().hex[:12]}"
    maintenance = _dsn_for_database(base, "postgres")
    target = _dsn_for_database(base, database_name)
    with connect(maintenance, autocommit=True) as connection:
        connection.execute(SQL("CREATE DATABASE {} ").format(Identifier(database_name)))
    try:
        monkeypatch.setenv("IPC_DATABASE_URL", target)
        config = Config("alembic.ini")
        config.attributes["ipc_database_url"] = target
        config.set_main_option("sqlalchemy.url", target)
        command.upgrade(config, "20260807_0002")
        with connect(target) as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,source,name,spec_json,spec_digest,created_at,updated_at)
                   VALUES ('legacy-workflow','legacy','Legacy','{}','legacy-digest',now(),now())"""
            )
            connection.commit()
        command.upgrade(config, "head")
        with connect(target) as connection:
            workflow = connection.execute(
                "SELECT id,name FROM workflows WHERE id='legacy-workflow'"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
                ).fetchall()
            }
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert workflow == ("legacy-workflow", "Legacy")
        assert {
            "competition_runs",
            "competition_challenges",
            "competition_sessions",
            "competition_artifacts",
            "competition_run_observations",
            "agent_sessions",
            "agent_session_writers",
            "agent_events",
            "agent_turns",
            "agent_compactions",
            "agent_jobs",
        }.issubset(tables)
        assert revision == _head_revision(config)
    finally:
        with connect(maintenance, autocommit=True) as connection:
            connection.execute(
                SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(database_name))
            )


def _fresh_database(monkeypatch, prefix: str):
    """Create a throwaway database and an Alembic config pointed at it."""
    base = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("set IPC_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    maintenance = _dsn_for_database(base, "postgres")
    target = _dsn_for_database(base, database_name)
    with connect(maintenance, autocommit=True) as connection:
        connection.execute(SQL("CREATE DATABASE {} ").format(Identifier(database_name)))
    monkeypatch.setenv("IPC_DATABASE_URL", target)
    config = Config("alembic.ini")
    config.attributes["ipc_database_url"] = target
    config.set_main_option("sqlalchemy.url", target)
    return target, maintenance, database_name, config


def _drop_database(maintenance: str, database_name: str) -> None:
    with connect(maintenance, autocommit=True) as connection:
        connection.execute(
            SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(database_name))
        )


def test_several_active_sessions_on_one_challenge_do_not_abort_the_migration(
    monkeypatch,
):
    """A real history has one active session per Member on the same challenge.

    ``uq_agent_session_active_task`` permits exactly one live root session per
    task, so the backfill has to resolve the contention itself. The session
    still holding an unreleased, unexpired assignment is the one a worker may
    be running, so it has to be the survivor - picking the newest row instead
    would retire the live seat.
    """
    target, maintenance, database_name, config = _fresh_database(
        monkeypatch, "ipc_multi"
    )
    try:
        command.upgrade(config, "20260925_0008")
        with connect(target) as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,source,name,spec_json,spec_digest,created_at,updated_at)
                   VALUES ('wf','legacy','WF','{}','digest',now(),now())"""
            )
            connection.execute(
                """INSERT INTO competition_runs
                   (id,workflow_id,identity_key,idempotency_key,config_snapshot,status)
                   VALUES ('run','wf','platform/game/team','idem','{}','running')"""
            )
            connection.execute(
                """INSERT INTO competition_challenges
                   (id,identity_key,external_id,run_id,state,first_assigned_at,deadline_at)
                   VALUES ('chal','platform/game/team','1','run','solving',
                           now(),now()+interval '5 hours')"""
            )
            connection.execute(
                "INSERT INTO competition_run_challenges (run_id,challenge_id) VALUES ('run','chal')"
            )
            connection.execute(
                """INSERT INTO competition_sessions
                   (id,run_id,challenge_id,member,state,next_sequence,created_at)
                   VALUES ('sess-live','run','chal','amber','active',1,
                           now()-interval '30 minutes'),
                          ('sess-stale','run','chal','agate','active',1,
                           now()-interval '10 minutes'),
                          ('sess-newest','run','chal','topaz','active',1, now())"""
            )
            connection.execute(
                """INSERT INTO competition_assignments
                   (id,run_id,challenge_id,session_id,member,role,position,
                    lease_owner,lease_expires_at)
                   VALUES ('a-live','run','chal','sess-live','amber','primary',1,
                           'worker-1',now()+interval '60 seconds'),
                          ('a-expired','run','chal','sess-stale','agate','helper',2,
                           'worker-2',now()-interval '10 minutes')"""
            )
            connection.commit()

        command.upgrade(config, "head")

        with connect(target) as connection:
            states = dict(
                connection.execute(
                    "SELECT id,state FROM agent_sessions ORDER BY id"
                ).fetchall()
            )
            notes = connection.execute(
                """SELECT session_id, payload->>'status', payload->>'had_live_assignment'
                   FROM agent_events
                   WHERE event_key='migration:20260929_0009:superseded'
                   ORDER BY session_id"""
            ).fetchall()
            sequences = dict(
                connection.execute(
                    "SELECT id,next_sequence FROM agent_sessions ORDER BY id"
                ).fetchall()
            )

        assert states == {
            "sess-live": "active",
            "sess-newest": "superseded",
            "sess-stale": "superseded",
        }
        assert notes == [
            ("sess-newest", "superseded_by_migration", "false"),
            ("sess-stale", "superseded_by_migration", "false"),
        ]
        assert sequences["sess-stale"] == 2
        assert sequences["sess-live"] == 1
    finally:
        _drop_database(maintenance, database_name)


def test_event_kinds_outside_the_new_enum_are_preserved_as_status(monkeypatch):
    """The legacy kind column had no CHECK, so unknown kinds really do exist.

    They must not abort the migration nor lose their original name: the kind
    becomes ``status`` and the real one is kept in the payload.
    """
    target, maintenance, database_name, config = _fresh_database(
        monkeypatch, "ipc_kinds"
    )
    try:
        command.upgrade(config, "20260925_0008")
        with connect(target) as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,source,name,spec_json,spec_digest,created_at,updated_at)
                   VALUES ('wf','legacy','WF','{}','digest',now(),now())"""
            )
            connection.execute(
                """INSERT INTO competition_runs
                   (id,workflow_id,identity_key,idempotency_key,config_snapshot,status)
                   VALUES ('run','wf','platform/game/team','idem','{}','running')"""
            )
            connection.execute(
                """INSERT INTO competition_sessions (id,run_id,member,next_sequence)
                   VALUES ('sess','run','amber',4)"""
            )
            connection.execute(
                """INSERT INTO competition_session_events
                   (session_id,sequence,event_key,kind,payload)
                   VALUES ('sess',1,'e1','member_thought','{"text":"hmm"}'::jsonb),
                          ('sess',2,'e2','difficulty_report','{"score":3}'::jsonb),
                          ('sess',3,'e3','tool_call','{"id":"c1"}'::jsonb)"""
            )
            connection.commit()

        command.upgrade(config, "head")

        with connect(target) as connection:
            rows = connection.execute(
                """SELECT sequence, kind, payload->>'legacy_kind', payload->'payload'
                   FROM agent_events WHERE session_id='sess' ORDER BY sequence"""
            ).fetchall()

        assert rows[0] == (1, "status", "member_thought", {"text": "hmm"})
        assert rows[1] == (2, "status", "difficulty_report", {"score": 3})
        assert rows[2][:3] == (3, "tool_call", None)
    finally:
        _drop_database(maintenance, database_name)


def test_existing_competition_history_is_backfilled_and_stays_queryable(monkeypatch):
    """Upgrade a database that already holds a run, then read the children.

    The risk in this revision is the foreign-key re-point: five tables
    reference the session. This exercises the real ordering - legacy rows
    first, then ``upgrade`` - and asserts the transcript moved to
    ``agent_events`` while submissions, WP jobs and recon cursors remain
    joinable.
    """
    base = os.environ.get("IPC_TEST_DATABASE_URL", "").strip()
    if not base:
        pytest.skip("set IPC_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database_name = f"ipc_backfill_{uuid.uuid4().hex[:12]}"
    maintenance = _dsn_for_database(base, "postgres")
    target = _dsn_for_database(base, database_name)
    with connect(maintenance, autocommit=True) as connection:
        connection.execute(SQL("CREATE DATABASE {} ").format(Identifier(database_name)))
    try:
        monkeypatch.setenv("IPC_DATABASE_URL", target)
        config = Config("alembic.ini")
        config.attributes["ipc_database_url"] = target
        config.set_main_option("sqlalchemy.url", target)
        command.upgrade(config, "20260925_0008")

        with connect(target) as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,source,name,spec_json,spec_digest,created_at,updated_at)
                   VALUES ('wf','legacy','WF','{}','digest',now(),now())"""
            )
            connection.execute(
                """INSERT INTO competition_runs
                   (id,workflow_id,identity_key,idempotency_key,config_snapshot,status)
                   VALUES ('run','wf','platform/game/team','idem','{}','running')"""
            )
            connection.execute(
                """INSERT INTO competition_challenges
                   (id,identity_key,external_id,run_id,state,first_assigned_at,deadline_at)
                   VALUES ('chal','platform/game/team','1','run','solving',
                           now(),now()+interval '5 hours')"""
            )
            connection.execute(
                "INSERT INTO competition_run_challenges (run_id,challenge_id) VALUES ('run','chal')"
            )
            connection.execute(
                """INSERT INTO competition_sessions (id,run_id,challenge_id,member,next_sequence)
                   VALUES ('sess','run','chal','amber',3)"""
            )
            connection.execute(
                """INSERT INTO competition_session_events
                   (session_id,sequence,event_key,kind,payload)
                   VALUES ('sess',1,'session:user:initial','provider_messages',
                           '{"messages":[{"role":"user","content":"start"}]}'::jsonb),
                          ('sess',2,'compact:1','context_compacted',
                           '{"summary":"legacy summary","messages":[]}'::jsonb)"""
            )
            connection.execute(
                """INSERT INTO competition_assignments
                   (id,run_id,challenge_id,session_id,member,role,position,
                    lease_owner,lease_expires_at)
                   VALUES ('assign','run','chal','sess','amber','primary',1,
                           'worker',now()+interval '60 seconds')"""
            )
            connection.execute(
                """INSERT INTO competition_submissions
                   (id,challenge_id,run_id,session_id,member,candidate,candidate_hash,
                    evidence,instance_generation,status)
                   VALUES ('sub','chal','run','sess','amber','flag{x}','hash',
                           'evidence',0,'correct')"""
            )
            connection.execute(
                """INSERT INTO competition_wp_jobs (challenge_id,run_id,member,session_id)
                   VALUES ('chal','run','amber','sess')"""
            )
            connection.execute(
                "INSERT INTO competition_message_cursors (session_id,sequence) VALUES ('sess',7)"
            )
            connection.execute(
                """INSERT INTO competition_artifacts
                   (challenge_id,path,sha256,size_bytes,updated_by_session)
                   VALUES ('chal','shared/a.txt','sha',12,'sess')"""
            )
            connection.commit()

        command.upgrade(config, "head")

        with connect(target) as connection:
            session = connection.execute(
                "SELECT id,kind,task_key,state,next_sequence FROM agent_sessions WHERE id='sess'"
            ).fetchone()
            events = connection.execute(
                "SELECT sequence,kind FROM agent_events WHERE session_id='sess' ORDER BY sequence"
            ).fetchall()
            compaction = connection.execute(
                """SELECT up_to_sequence, summary->>'text', summary->>'legacy'
                   FROM agent_compactions WHERE session_id='sess'"""
            ).fetchone()
            writer = connection.execute(
                "SELECT owner,epoch FROM agent_session_writers WHERE session_id='sess'"
            ).fetchone()
            children = connection.execute(
                """SELECT s.id, sub.id, wp.challenge_id, cur.sequence, art.path
                   FROM agent_sessions s
                   JOIN competition_assignments a ON a.session_id=s.id
                   JOIN competition_submissions sub ON sub.session_id=s.id
                   JOIN competition_wp_jobs wp ON wp.session_id=s.id
                   JOIN competition_message_cursors cur ON cur.session_id=s.id
                   JOIN competition_artifacts art ON art.updated_by_session=s.id
                   WHERE s.id='sess'"""
            ).fetchone()
            legacy_kept = connection.execute(
                "SELECT count(*) FROM competition_session_events WHERE session_id='sess'"
            ).fetchone()[0]
            member_pinning = connection.execute(
                """SELECT count(*) FROM pg_constraint
                   WHERE conrelid='competition_assignments'::regclass AND contype='f'
                     AND confrelid='competition_sessions'::regclass"""
            ).fetchone()[0]

        assert session == ("sess", "competition", "competition:run:chal", "active", 3)
        assert events == [(1, "provider_messages"), (2, "context_compacted")]
        assert compaction == (2, "legacy summary", "true")
        assert writer == ("worker", 1)
        assert children == ("sess", "sub", "chal", 7, "shared/a.txt")
        assert legacy_kept == 2
        assert member_pinning == 0
    finally:
        with connect(maintenance, autocommit=True) as connection:
            connection.execute(
                SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(database_name))
            )
