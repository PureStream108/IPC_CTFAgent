from types import SimpleNamespace

import pytest

from backend.competition.service import (
    CompetitionService,
    _select_discovered,
    _selected_external_ids,
)


def test_select_discovered_keeps_only_requested_challenges():
    discovered = [
        SimpleNamespace(external_id="1129"),
        SimpleNamespace(external_id="1135"),
        SimpleNamespace(external_id="1208"),
    ]

    assert _select_discovered(discovered, None) == discovered

    selected = _select_discovered(discovered, ["1208", "1129"])
    assert [item.external_id for item in selected] == ["1129", "1208"]

    with pytest.raises(ValueError, match="not available: 9999"):
        _select_discovered(discovered, ["1129", "9999"])


def test_selected_external_ids_keeps_platform_sync_bounded():
    assert _selected_external_ids({"config_snapshot": {}}) is None
    assert _selected_external_ids(
        {"config_snapshot": {"selected_external_ids": [1129, "1208"]}}
    ) == {"1129", "1208"}


def test_redispatch_retries_when_bootstrap_markers_linger():
    service = object.__new__(CompetitionService)
    service.owner = "coordinator"
    service._starting_members = {("proj_1", "amber"), ("proj_1", "jade")}
    service._starting_projects = {"proj_1"}
    service._engine_conflicts = set()
    service._engine_epochs = {}
    service._claim_project_engine = lambda _run, _project_id: True

    class _Orchestrator:
        def __init__(self):
            self.started: list[str] = []
            self._starting: set[str] = set()

        def active_member_owners(self):
            return {}

        def startup_in_progress(self, project_id):
            return project_id in self._starting

        def start_project_async(self, project_id):
            self.started.append(project_id)
            self._starting.add(project_id)
            return {"project_id": project_id, "status": "queued"}

    orchestrator = _Orchestrator()
    service.state = SimpleNamespace(orchestrator=orchestrator)
    service.store = SimpleNamespace(
        challenges=lambda _run_id: [{"id": "challenge_1", "project_id": "proj_1"}],
        assignments=lambda _run_id, active_only=False: [
            {
                "challenge_id": "challenge_1",
                "member": "amber",
                "role": "primary",
                "lease_owner": "coordinator",
                "epoch": 1,
            },
            {
                "challenge_id": "challenge_1",
                "member": "jade",
                "role": "helper",
                "lease_owner": "coordinator",
                "epoch": 1,
            },
        ],
        append_run_event=lambda *_args, **_kwargs: None,
    )
    run = {"id": "run_1"}

    service._sync_orchestrator_assignments(run)
    assert orchestrator.started == ["proj_1"]

    orchestrator._starting.clear()
    service._sync_orchestrator_assignments(run)
    assert orchestrator.started == ["proj_1", "proj_1"]


def test_start_rejects_platform_without_submit_capability_before_persisting_run():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(
        config=SimpleNamespace(startup_errors=lambda: []),
    )
    service._workflow = lambda _workflow_id: {
        "id": "workflow",
        "spec": SimpleNamespace(),
        "spec_digest": "digest",
    }
    service._platform_for_workflow = lambda _workflow: SimpleNamespace(
        identity="readonly://platform",
        supports_submit=False,
        preflight=lambda: [],
    )
    service.store = SimpleNamespace(
        create_run=lambda *_args, **_kwargs: pytest.fail(
            "read-only platform must be rejected before run persistence"
        )
    )

    with pytest.raises(ValueError, match="does not support flag submission"):
        service.start("workflow", "run-key")
