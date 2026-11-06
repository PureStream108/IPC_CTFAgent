from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.members.adapters import MemberAction
from backend.members.base_member import BaseMember, MemberDeps


class Logger:
    def __init__(self):
        self.events = []

    def project(self, event, *args, **kwargs):
        self.events.append((event, args, kwargs))

    def tool(self, *args, **kwargs):
        self.events.append((args[0] if args else "tool", args, kwargs))


class Adapter:
    config = SimpleNamespace(api_format="mock")

    def turn(self, *args):
        return None


def member(**changes):
    logger = changes.pop("logger", Logger())
    values = {
        "db": None,
        "logger": logger,
        "sandbox": SimpleNamespace(),
        "mcps": SimpleNamespace(names=list),
        "registry": SimpleNamespace(search=lambda *_args, **_kwargs: []),
        "memory": None,
        "competition_store": None,
        "competition_assignment": None,
    }
    values.update(changes)
    return BaseMember("amber", Adapter(), MemberDeps(**values)), logger


def test_streaming_enabled_prefers_native_competition():
    solver, _logger = member(
        competition_native=True,
        competition_store=object(),
        competition_assignment={"session_id": "session-1"},
    )
    solver.adapter.config = SimpleNamespace(api_format="openai")
    assert solver._streaming_enabled() == (True, "competition_native")


def test_skill_read_dispatch_returns_body_and_next_offset():
    class Store:
        def read_skill(self, name, relative, category=None):
            return {
                "name": name,
                "path": "ctf-web/SKILL.md",
                "content": "abcdef",
                "repository": "acme/skills",
                "revision": "a" * 40,
            }

    solver, logger = member(skills=Store())
    result = solver._dispatch_skill_read(
        "proj_001",
        "intent-1",
        "web",
        MemberAction(kind="skill_read", args={"name": "web-expert", "limit": 4}),
    )

    assert result.invalid_action is False
    assert result.observation.startswith("[skill:web-expert] ctf-web/SKILL.md")
    assert "abcd" in result.observation
    assert "offset=4" in result.observation
    assert any(event[0] == "skill_read" for event in logger.events)


def test_skill_read_dispatch_reports_unknown_skill():
    class Store:
        def read_skill(self, name, relative, category=None):
            raise KeyError("unknown skill: nope")

    solver, _logger = member(skills=Store())
    result = solver._dispatch_skill_read(
        "proj_001", "intent-1", "web", MemberAction(kind="skill_read", args={"name": "nope"})
    )

    assert result.invalid_action is True
    assert "unknown skill" in result.observation


def test_member_before_turn_delivers_job_digest():
    solver, _logger = member()
    solver._drain_competition_recon = lambda runner: []

    class Store:
        def curated_digests(self, project_id, member, limit=3):
            return [{
                "id": "job-2",
                "member": member,
                "command": "sleep 120",
                "state": "exited",
                "exit_code": 0,
                "digest_json": {"summary_zh": "长任务完成", "key_findings": []},
            }]

        def mark_digests_delivered(self, ids):
            return len(ids)

    solver.deps.job_store = Store()
    messages = solver._drain_before_turn(None, "proj_001")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "Sugilite" in messages[0]["content"]
    assert "长任务完成" in messages[0]["content"]


def test_member_consumes_sugilite_job_digests():
    solver, _logger = member()
    delivered = []

    class Store:
        def curated_digests(self, project_id, member, limit=3):
            return [{
                "id": "job-1",
                "member": member,
                "command": "nmap -p- target",
                "state": "exited",
                "exit_code": 0,
                "digest_json": {
                    "summary_zh": "扫描完成",
                    "key_findings": [{"type": "endpoint", "value": "22/tcp open ssh"}],
                    "suggested_next": ["继续验证"],
                },
            }]

        def mark_digests_delivered(self, ids):
            delivered.extend(ids)
            return len(ids)

    solver.deps.job_store = Store()
    digests = solver._consume_job_digests("proj_001")
    assert len(digests) == 1
    assert "Sugilite" in digests[0]
    assert "扫描完成" in digests[0]
    assert "22/tcp open ssh" in digests[0]
    assert delivered == ["job-1"]


def test_member_session_job_and_recovery_seams(monkeypatch):
    solver, logger = member()
    assert solver._jobs() is None
    assert solver._competition_session() is None
    assert solver._renew_session_writer() is False
    assert solver._record_competition_event(lambda _runner: None) is True

    class JobSandbox:
        def exec_detached(self, command):
            return command

    solver.deps.job_store = object()
    solver.deps.sandbox = JobSandbox()
    solver.deps.competition_assignment = {"session_id": "session-1"}
    assert solver._jobs() is not None

    class Writer:
        def renew(self):
            return True

    solver._session_runner = SimpleNamespace(writer=Writer())
    assert solver._renew_session_writer() is True

    class BrokenWriter:
        def renew(self):
            raise RuntimeError("lost")

    solver._session_runner = SimpleNamespace(writer=BrokenWriter())
    assert solver._renew_session_writer() is False
    solver._session_runner = SimpleNamespace(writer=Writer())
    assert solver._record_competition_event(lambda _runner: (_ for _ in ()).throw(RuntimeError("db"))) is False
    assert solver._session_recording_failed is True
    assert any(event[0] == "competition_session_persistence_failed" for event in logger.events)


def test_member_recon_drain_persists_before_ack(monkeypatch):
    solver, _logger = member(
        competition_assignment={"challenge_id": "ch", "session_id": "s"},
        competition_recon_replay=lambda *_args, **_kwargs: [],
        competition_recon_advance=lambda _session, sequence: sequence,
    )

    class Subscriber:
        def __init__(self):
            self.acked = []
            self.records = [
                {"sequence": 3, "envelope": {"payload": {"hint": "use /admin"}}},
                None,
            ]

        def receive(self, timeout_ms=0):
            return self.records.pop(0)

        def ack(self, sequence):
            self.acked.append(sequence)

        def close(self):
            return None

    subscriber = Subscriber()
    monkeypatch.setattr("backend.members.base_member.ReconSubscriber", lambda *args, **kwargs: subscriber)
    runner = SimpleNamespace(record_provider_message=lambda message, event_key: setattr(runner, "message", (message, event_key)))
    messages = solver._drain_competition_recon(runner)
    assert messages[0]["role"] == "user"
    assert subscriber.acked == [3]
    assert runner.message[1] == "recon:3"


def test_member_driver_selection_and_argument_helpers():
    solver, _logger = member()
    assert solver._streaming_enabled() == (True, "mock_stream")
    solver.adapter = SimpleNamespace(config=SimpleNamespace(api_format="openai"), turn=lambda *args: None)
    assert solver._streaming_enabled() == (True, "provider_native")
    solver.adapter = SimpleNamespace(config=SimpleNamespace(api_format="mock"), decide=lambda *_args: None)
    assert solver._streaming_enabled() == (False, "adapter_without_turn_surface")
    assert solver._missing_command_from_result("python3 solve.py", "bash: nope: command not found") == "nope"
    assert solver._missing_command_from_result("python3", "'tool.exe' is not recognized") == "tool.exe"
    assert solver._missing_command_from_result("python3", "") is None
    solver.bump("new evidence")
    solver.bump(" ")
    assert solver._consume_bumps() == ["new evidence"]
    assert solver._consume_bumps() == []
    assert solver._native_action_tool()["name"] == "member_action"


@pytest.mark.parametrize("kind,args", [
    ("job_status", {}),
    ("job_status", {"job_id": "j1"}),
    ("job_logs", {"job_id": "j1", "offset": "2", "limit": 3}),
    ("job_cancel", {"job_id": "j1"}),
    ("job_start", {"command": "echo hi"}),
])
def test_member_detached_job_dispatch_contract(kind, args):
    class Jobs:
        def jobs(self): return {"ok": True, "state": "listed"}
        def status(self, job_id): return {"ok": True, "job_id": job_id, "state": "running"}
        def logs(self, job_id, **kwargs): return {"ok": True, "job_id": job_id, **kwargs}
        def cancel(self, job_id): return {"ok": True, "job_id": job_id, "state": "cancelled"}
        def start(self, command): return {"ok": True, "job_id": "new", "command": command}

    solver, _logger = member()
    solver._job_service = Jobs()
    result = solver._dispatch_job("project", "intent", MemberAction(kind=kind, args=args))
    assert result.invalid_action is False
    if kind == "job_logs":
        assert "offset" in (result.observation or "")


def test_member_invalid_detached_job_arguments_are_rejected():
    solver, _logger = member()
    result = solver._dispatch_job("project", "intent", MemberAction(kind="job_status", args={}))
    assert result.invalid_action is True
    solver._job_service = None
    result = solver._dispatch_job("project", "intent", MemberAction(kind="job_start", args={"command": ""}))
    assert result.invalid_action is True
