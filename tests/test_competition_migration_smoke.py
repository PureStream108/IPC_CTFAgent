from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from psycopg import connect
from psycopg.sql import Identifier, SQL


pytestmark = pytest.mark.postgres


def _dsn_for_database(base: str, database: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.netloc:
        raise ValueError("IPC_TEST_DATABASE_URL must be a PostgreSQL URL")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))


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
        # Keep both the repository-specific attribute and Alembic's standard
        # URL populated.  This also works with older Alembic runners that
        # clone Config before entering env.py.
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
        }.issubset(tables)
        assert revision == "20260929_0009"
    finally:
        with connect(maintenance, autocommit=True) as connection:
            connection.execute(
                SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(database_name))
            )


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
            # Every child row still resolves through the re-pointed keys.
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
            # The composite key pinning a session to one member is gone, so a
            # different Member can take the seat without losing the transcript.
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
