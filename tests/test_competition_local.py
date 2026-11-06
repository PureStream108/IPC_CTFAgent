from __future__ import annotations

import pytest

from backend.competition.local import LocalPlatform


class FakeDB:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_local_platform_exposes_existing_project_without_provisioning():
    platform = LocalPlatform(
        {
            "project_id": "proj_local",
            "title": "Local challenge",
            "category": "web",
            "goal": "Capture the local flag",
            "solved": True,
        },
        db=FakeDB(),
    )

    assert platform.external_id == "proj_local"
    assert platform.identity == "local://proj_local"
    assert platform.supports_submit is True
    assert platform.supports_instances is False
    assert platform.instance_limit() is None
    assert platform.network_diagnostics() == {"reachable": True, "source": "local"}
    challenge = platform.preflight()[0]
    assert challenge.external_id == "proj_local"
    assert challenge.title == "Local challenge"
    assert challenge.category == "web"
    assert challenge.description == "Capture the local flag"
    assert challenge.remote is False
    assert challenge.solved is True
    assert platform.instances() == []
    assert platform.query("proj_local", "submission") == {
        "verdict": "unknown",
        "submission_id": "submission",
    }
    assert platform.stop_instance("proj_local") == {
        "external_id": "proj_local",
        "state": "stopped",
    }
    for operation in (
        platform.start_instance,
        platform.renew_instance,
        platform.rebuild_instance,
    ):
        with pytest.raises(RuntimeError, match="do not provision"):
            operation("proj_local")


def test_local_platform_without_project_has_no_challenges():
    platform = LocalPlatform(None, db=FakeDB())

    assert platform.external_id == ""
    assert platform.challenges() == []
    assert platform.preflight() == []


def test_local_submit_uses_durable_flag_path(monkeypatch):
    calls = []

    def accept(connection, project_id, flag, *, source):
        calls.append((connection, project_id, flag, source))
        return {"flag": "flag{verified}"}

    monkeypatch.setattr("backend.core.ipc.submit_flag_candidate", accept)
    database = FakeDB()
    platform = LocalPlatform({"project_id": "proj_local"}, db=database)

    assert platform.submit("proj_local", "flag{verified}") == {
        "verdict": "correct",
        "submission_id": "flag{verified}",
    }
    assert calls == [(database, "proj_local", "flag{verified}", "competition")]


def test_local_submit_distinguishes_unknown_and_wrong_candidates(monkeypatch):
    calls = []

    def reject(connection, project_id, flag, *, source):
        calls.append((project_id, flag, source))
        raise ValueError("candidate did not match")

    monkeypatch.setattr("backend.core.ipc.submit_flag_candidate", reject)
    platform = LocalPlatform({"project_id": "proj_local"}, db=FakeDB())

    assert platform.submit("proj_other", "flag{unknown}") == {
        "verdict": "rejected",
        "message": "unknown local challenge",
    }
    assert platform.submit("proj_local", "flag{wrong}") == {
        "verdict": "wrong",
        "message": "candidate did not match",
    }
    assert calls == [("proj_local", "flag{wrong}", "competition")]
