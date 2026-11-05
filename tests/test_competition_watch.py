"""The watcher opens a run for the first takeable challenge of a match.

A CompetitionRun imports every challenge it discovers on each sync, so the only
window the watcher has to cover is "no run exists yet".  These tests pin that
window: it must start exactly one run, must not select a subset (a selected run
ignores everything published later), and must not resurrect a run while another
one is active.
"""

from __future__ import annotations

from backend.competition.watcher import PlatformWatcher
from backend.ops.models import PlatformWorkflowSpec
from backend.platform.mapping import PlatformChallenge


def workflow(workflow_id: str = "wf_watch", *, attachments_only: bool = True, status: str = "confirmed"):
    spec = PlatformWorkflowSpec.model_validate(
        {
            "name": "watch fixture",
            "competition_id": "match",
            "team_id": "team",
            "challenges": {
                "platform": "ichunqiu",
                "list_url": "https://match.example.test",
                "attachments_only": attachments_only,
            },
        }
    )
    return {"id": workflow_id, "status": status, "spec": spec}


def challenge(external_id: str = "1001", *, remote: bool = False, solved: bool = False):
    return PlatformChallenge(
        external_id=external_id,
        title=f"challenge {external_id}",
        category="misc",
        description="",
        remote=remote,
        solved=solved,
    )


class FakeStore:
    def __init__(self, active=None):
        self.active = active

    def active_run(self):
        return self.active


class FakeOpsStore:
    def __init__(self, workflows):
        self.workflows = workflows

    def list_workflows(self):
        return list(self.workflows)


class FakeOps:
    def __init__(self, workflows):
        self.store = FakeOpsStore(workflows)


class FakePlatform:
    def __init__(self, challenges=(), error=None):
        self._challenges = list(challenges)
        self._error = error
        self.calls = 0

    def challenges(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._challenges)


class FakeService:
    def __init__(self, workflows, platform, active=None):
        self.store = FakeStore(active)
        self.ops = FakeOps(workflows)
        self.platform = platform
        self.started: list[tuple[str, str, list[str] | None]] = []
        self.refreshed: list[str] = []

    def platform_for_workflow(self, workflow_id):
        return self.platform

    def start(self, workflow_id, idempotency_key, select=None):
        self.started.append((workflow_id, idempotency_key, select))
        return {"run": {"id": "run_new"}}

    def refresh(self, run_id):
        self.refreshed.append(run_id)
        return {"challenge_count": 2, "new_count": 1}


def test_starts_one_run_for_the_first_takeable_challenge():
    service = FakeService([workflow()], FakePlatform([challenge("1001")]))

    result = PlatformWatcher(service, interval=60).tick()

    assert result["ok"] is True
    assert service.started == [("wf_watch", service.started[0][1], None)]
    # A selected run would freeze its challenge set and ignore later releases.
    assert service.started[0][2] is None
    assert service.started[0][1].startswith("watch-wf_watch-")
    assert "started run run_new" in result["action"]


def test_idles_while_the_platform_exposes_nothing_takeable():
    service = FakeService([workflow()], FakePlatform([]))

    result = PlatformWatcher(service, interval=60).tick()

    assert result["action"] == "no takeable challenge"
    assert service.started == []


def test_ignores_a_workflow_that_is_not_attachment_only():
    service = FakeService([workflow(attachments_only=False)], FakePlatform([challenge()]))

    result = PlatformWatcher(service, interval=60).tick()

    assert service.started == []
    assert result["action"] == "no takeable challenge"


def test_ignores_an_unconfirmed_workflow():
    service = FakeService([workflow(status="draft")], FakePlatform([challenge()]))

    PlatformWatcher(service, interval=60).tick()

    assert service.started == []


def test_workflow_allowlist_excludes_other_workflows():
    service = FakeService(
        [workflow("wf_a"), workflow("wf_b")], FakePlatform([challenge()])
    )

    PlatformWatcher(service, interval=60, workflow_ids=["wf_b"]).tick()

    assert [item[0] for item in service.started] == ["wf_b"]


def test_active_run_is_refreshed_instead_of_starting_a_second():
    active = {"id": "run_live", "workflow_id": "wf_watch", "status": "running"}
    service = FakeService([workflow()], FakePlatform([challenge()]), active=active)

    result = PlatformWatcher(service, interval=60).tick()

    assert service.started == []
    assert service.refreshed == ["run_live"]
    assert "2 visible, 1 new" in result["action"]


def test_active_run_of_another_workflow_is_left_alone():
    active = {"id": "run_other", "workflow_id": "wf_other", "status": "running"}
    service = FakeService([workflow()], FakePlatform([challenge()]), active=active)

    result = PlatformWatcher(service, interval=60).tick()

    assert service.started == []
    assert service.refreshed == []
    assert "owns another workflow" in result["action"]


def test_paused_run_is_not_refreshed():
    active = {"id": "run_live", "workflow_id": "wf_watch", "status": "paused"}
    service = FakeService([workflow()], FakePlatform([challenge()]), active=active)

    result = PlatformWatcher(service, interval=60).tick()

    assert service.refreshed == []
    assert "is paused" in result["action"]


def test_platform_error_is_reported_and_the_loop_survives():
    service = FakeService([workflow()], FakePlatform(error=RuntimeError("token expired")))
    watcher = PlatformWatcher(service, interval=60)

    first = watcher.tick()
    second = watcher.tick()

    assert first["ok"] is False
    assert "token expired" in first["error"]
    assert second["ok"] is False
    assert watcher.snapshot()["last_error"].endswith("token expired")
    assert watcher.snapshot()["ticks"] == 2


def test_snapshot_reports_the_watch_targets():
    service = FakeService([workflow()], FakePlatform([]))
    watcher = PlatformWatcher(service, interval=90, workflow_ids=["wf_watch"])

    watcher.tick()

    snapshot = watcher.snapshot()
    assert snapshot["interval_seconds"] == 90
    assert snapshot["workflows"] == ["wf_watch"]
    assert snapshot["ticks"] == 1
    assert snapshot["enabled"] is False
