from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from backend.agent.jobs import AgentJobStore
from backend.agent.session import AgentSessionStore
from backend.blackboard import graph_store
from backend.core.config import AppConfig, LLMConfig
from backend.core.curator import SugiliteCurator, curator_scope, is_curator_project
from backend.core.diamond import Diamond
from backend.core.orchestrator import Orchestrator
from backend.persistence.database import Database

pytestmark = pytest.mark.postgres


class Logger:
    def project(self, *args, **kwargs):
        return None


class Sandbox:
    def job_output(self, job_id, stream="stdout", *, offset=0, limit=8192):
        text = "flag{demo-flag}\n22/tcp open ssh\n" if stream == "stdout" else ""
        return text[offset : offset + limit], min(len(text), offset + limit)

    def job_state(self, job_id):
        return {"state": "exited", "exit_code": 0}


def test_curator_project_and_job_digest_round_trip():
    database = Database().configure()
    try:
        with database.connect() as connection:
            project_id = graph_store.create_project(
                connection, "curator test", "origin", "goal", "web"
            )
        session = AgentSessionStore(database).create_session(
            "project", f"projects:{project_id}:amber"
        )
        jobs = AgentJobStore(database)
        job = jobs.start(
            session["id"], "nmap -p- target",
            project_id=project_id, member="amber", intent_id="i001",
        )
        jobs.finish(job["id"], "exited", exit_code=0)

        config = AppConfig(member=LLMConfig(api_format="mock"))
        diamond = Diamond(database, config, Logger())
        assert is_curator_project(database, project_id) is True
        assert "sugilite" not in {member.name for member in diamond._solver_members(project_id)}

        class Adapter:
            def chat(self, *args, **kwargs):
                return json.dumps({
                    "summary_zh": "扫描完成",
                    "verdict": "success",
                    "key_findings": [{"type": "endpoint", "value": "22/tcp", "confidence": 0.8}],
                })

        curator = SugiliteCurator(
            database, Logger(), config, project_id,
            sandbox_provider=lambda pid, member: Sandbox(),
            adapter_factory=lambda member_config, name: Adapter(),
        )
        jobs.claim_digest(job["id"])
        curator._curate(jobs.job(job["id"]))
        curator.close()
        rows = jobs.curated_digests(project_id, "amber")
        assert rows
        digest = rows[0]["digest_json"]
        assert digest["source"] == "sugilite"
        assert digest["summary_zh"] == "扫描完成"
        assert any(item["type"] == "flag" for item in digest["key_findings"])
    finally:
        database.close()


def test_competition_project_gets_curator_without_losing_sugilite_member():
    database = Database().configure()
    try:
        suffix = uuid.uuid4().hex[:10]
        with database.connect() as connection:
            standalone_id = graph_store.create_project(
                connection, f"standalone-{suffix}", "origin", "goal", "web"
            )
            competition_id = graph_store.create_project(
                connection,
                f"competition-{suffix}",
                "origin",
                "goal",
                "web",
                platform="tsecbench",
            )
            connection.execute(
                """INSERT INTO competition_challenges
                       (id, identity_key, external_id, project_id, state)
                   VALUES (%s, %s, %s, %s, 'discovered')""",
                (
                    f"ch_{suffix}",
                    f"https://example.invalid/{suffix}",
                    f"g-{suffix}",
                    competition_id,
                ),
            )

        assert is_curator_project(database, standalone_id) is True
        assert curator_scope(database, standalone_id) is True
        assert is_curator_project(database, competition_id) is False
        assert curator_scope(database, competition_id) is True

        config = AppConfig(member=LLMConfig(api_format="mock"))
        solver_names = {
            member.name
            for member in Diamond(database, config, Logger())._solver_members(competition_id)
        }
        assert "sugilite" in solver_names
    finally:
        database.close()


def test_competition_curator_start_keeps_sugilite_member_role():
    database = Database().configure()
    try:
        suffix = uuid.uuid4().hex[:10]
        with database.connect() as connection:
            project_id = graph_store.create_project(
                connection,
                f"competition-curator-{suffix}",
                "origin",
                "goal",
                "web",
                platform="tsecbench",
            )
            connection.execute(
                """INSERT INTO competition_challenges
                       (id, identity_key, external_id, project_id, state)
                   VALUES (%s, %s, %s, %s, 'discovered')""",
                (
                    f"ch2_{suffix}",
                    f"https://example.invalid/{suffix}/two",
                    f"g2-{suffix}",
                    project_id,
                ),
            )
            graph_store.add_agent(connection, project_id, "sugilite", "member", state="active")

        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator._lock = threading.Lock()
        orchestrator._curators = {}
        orchestrator.state = SimpleNamespace(
            db=database,
            logger=Logger(),
            config=AppConfig(member=LLMConfig(api_format="mock")),
        )
        orchestrator._curator_sandbox = lambda pid, member: None

        curator = orchestrator._ensure_curator(project_id)
        assert curator is not None
        assert project_id in orchestrator._curators

        with database.connect() as connection:
            role = connection.execute(
                "SELECT role FROM agents WHERE project_id=%s AND name='sugilite'",
                (project_id,),
            ).fetchone()["role"]
        assert role == "member"

        orchestrator._dispose_curator(project_id)
    finally:
        database.close()
