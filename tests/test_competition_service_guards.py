import threading
from types import SimpleNamespace

import pytest

from backend.competition.service import (
    CompetitionService,
    _safe_member_config,
    _select_discovered,
    _selected_external_ids,
)
from backend.competition.store import CompetitionConflict


def test_quota_probe_failure_preserves_workflow_capacity():
    service = object.__new__(CompetitionService)
    service._instance_capability_warnings = set()
    events = []
    service.store = SimpleNamespace(append_run_event=lambda *args: events.append(args))

    class UnavailableQuota:
        supports_instances = True

        def instance_limit(self):
            raise ConnectionError("platform quota endpoint unavailable")

        def start_instance(self, _external_id):
            return {}

    run = {"id": "run", "limits": {}, "config_snapshot": {"workflow_spec": {"remote_instance_limit": 2}}}
    spec = run["config_snapshot"]["workflow_spec"]
    assert service._resolve_remote_limit(spec, UnavailableQuota()) == (2, "workflow.remote_instance_limit")
    assert service._resolve_remote_limit(spec, UnavailableQuota()) == (2, "workflow.remote_instance_limit")
    assert events == []


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
        config=SimpleNamespace(startup_errors=list),
    )
    service._workflow = lambda _workflow_id: {
        "id": "workflow",
        "spec": SimpleNamespace(),
        "spec_digest": "digest",
    }
    service._platform_for_workflow = lambda _workflow: SimpleNamespace(
        identity="readonly://platform",
        supports_submit=False,
        preflight=list,
    )
    service.store = SimpleNamespace(
        create_run=lambda *_args, **_kwargs: pytest.fail(
            "read-only platform must be rejected before run persistence"
        )
    )

    with pytest.raises(ValueError, match="does not support flag submission"):
        service.start("workflow", "run-key")


def test_capacity_and_instance_helpers_choose_explicit_sources_and_bounds():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(
        config=SimpleNamespace(limits=SimpleNamespace(max_concurrent_tasks=3)),
    )
    service._instance_capability_warnings = set()
    events = []
    service.store = SimpleNamespace(append_run_event=lambda *args: events.append(args))
    run = {
        "id": "run",
        "limits": {"remote_concurrency": 4, "max_concurrent_tasks": 2},
        "config_snapshot": {"workflow_spec": {"remote_concurrency": 2}},
    }

    class Reported:
        supports_instances = True

        def instance_limit(self):
            return 9

        def start_instance(self, _external_id):
            return {}

    assert service._remote_instance_limit(run, Reported()) == 4
    assert service._local_task_limit(run) == 2
    assert service._supports_instance_lifecycle(Reported()) is True
    assert service._supports_instance_lifecycle(SimpleNamespace(supports_instances=False)) is False
    assert service._instance_next_renewal({"ttl_seconds": 2})
    assert service._instance_next_renewal({"expires_at": "2099-01-01T00:00:00+00"})
    assert service._instance_next_renewal({"ttl_seconds": "bad"})

    fallback = {"id": "run-2", "limits": {}, "config_snapshot": {"workflow_spec": {}}}
    assert service._remote_instance_limit(fallback, Reported()) == 9
    assert service._remote_instance_limit(fallback, SimpleNamespace(supports_instances=False)) == 0
    assert events and events[-1][1] == "instance.limit_defaulted" or events == []


def test_member_snapshot_redacts_credentials_and_preserves_presence():
    assert _safe_member_config(None) is None
    config = SimpleNamespace(model_dump=lambda mode="json": {
        "api_key": "secret", "model": "m", "nested": {"token": "x"}
    })
    result = _safe_member_config(config)
    assert result["api_key_set"] is True
    assert result["api_key_fingerprint"]
    assert result["nested"]["token"] == "x"


def test_challenge_metadata_normalizes_remote_solve_count_and_container_type():
    from backend.platform.mapping import PlatformChallenge

    item = PlatformChallenge.model_validate({
        "external_id": "42", "title": "Dynamic", "category": "Web",
        "description": "target", "remote": True, "solved": True,
        "solve_count": "7", "platform_data": {"solve_count": "7", "container_type": "DynamicContainer", "score": "100"},
    })
    metadata = CompetitionService._challenge_metadata(item)
    assert metadata["remote"] is True
    assert metadata["solve_count"] == 7
    assert metadata["container_type"] == "DynamicContainer"
    assert metadata["score"] == 100
    malformed = CompetitionService._challenge_metadata(
        SimpleNamespace(external_id="x", title="x", category="misc", description="x",
                        remote=False, solved=False, attachment_urls=[], hints=[],
                        platform_data={"solve_count": "bad"})
    )
    assert "solve_count" not in malformed


def test_service_control_and_limit_update_use_revision_and_wake_scheduler():
    service = object.__new__(CompetitionService)
    service._tick_lock = threading.Lock()
    service._refresh_unlocked = lambda _run_id: None
    service.wake = lambda: setattr(service, "woken", True)
    service.store = SimpleNamespace(
        get_run=lambda _run_id: {"id": "run", "revision": 2, "status": "running"},
        run_snapshot=lambda _run_id: {"run": {"id": "run"}},
        update_limits=lambda run_id, limits, revision: {"id": run_id, "revision": revision + 1},
        append_run_event=lambda *_args: None,
    )
    service._teardown_stopped_run = lambda *_args: None
    service.store.transition_run = lambda *args, **kwargs: {"id": "run", "status": "paused"}
    assert service.control("run", "refresh", 2) == {"run": {"id": "run"}}
    with pytest.raises(ValueError, match="unsupported"):
        service.control("run", "unknown", 2)
    result = service.update_limits("run", {"remote_concurrency": 2}, 2)
    assert result == {"run": {"id": "run"}}
    assert service.woken is True

    class BusyLock:
        def acquire(self, **_kwargs):
            return False
        def release(self):
            return None
    service._tick_lock = BusyLock()
    with pytest.raises(CompetitionConflict, match="20 seconds") as caught:
        service.control("run", "refresh", 2)
    assert caught.value.retry_after == 20


def test_service_pair_and_import_compatibility_seams():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(competition_message_handler=None)
    message = SimpleNamespace(request_id="req")
    result = service._execute_pair_message(message, object())
    assert result["status"] == "failed"
    service.state.competition_message_handler = lambda msg, cancel: {"request_id": msg.request_id, "cancelled": cancel.is_set()}
    assert service._execute_pair_message(message, SimpleNamespace(is_set=lambda: False))["request_id"] == "req"

    calls = []
    def old_importer(workflow_id, spec, ids):
        calls.append((workflow_id, ids))
        return {"imported": []}
    service._ops_service = SimpleNamespace(_import=old_importer)
    assert service._import_for_run("wf", object(), ["1"], "run") == {"imported": []}
    assert calls == [("wf", ["1"])]
