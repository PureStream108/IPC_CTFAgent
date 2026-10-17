from __future__ import annotations

import json
from types import SimpleNamespace

from backend.agent.compaction import deterministic_summary, fit_history
from backend.competition.service import CompetitionService
from backend.ops.presets import GZCTFWorkflowInput, workflow_templates


def test_gzctf_preset_contains_only_platform_specific_inputs():
    assert workflow_templates()[0]["default"] is True
    settings = GZCTFWorkflowInput(
        base_url="https://ctf.example", game_id=33, name="Games 33",
        attachment_base_url="https://cdn.example", secrets={"gzctf_token": "secret"},
    )
    spec = settings.workflow_spec()
    assert spec.template_id == "gzctf"
    assert spec.challenges.platform == "gzctf"
    assert spec.challenges.game_id == 33
    assert spec.challenges.list_url == "https://ctf.example"
    assert spec.challenges.attachment_base_url == "https://cdn.example"


def test_control_queue_is_accepted_when_tick_lock_is_busy():
    service = object.__new__(CompetitionService)

    class BusyLock:
        def acquire(self, **_kwargs):
            return False

    service._tick_lock = BusyLock()
    service.wake = lambda: setattr(service, "woken", True)
    service.store = SimpleNamespace(
        queue_control=lambda run_id, action, revision, operation_id: {
            "id": operation_id or "op", "run_id": run_id, "action": action,
            "requested_revision": revision, "status": "queued",
        },
        run_snapshot=lambda _run_id: {"run": {"id": "run"}},
    )
    result = service.control("run", "pause", 2, operation_id="op-1")
    assert result["control_operation"]["id"] == "op-1"
    assert service.woken is True


def test_control_worker_records_success_and_failure_without_holding_tick_lock():
    service = object.__new__(CompetitionService)
    service.owner = "coordinator"
    calls = []
    queue = [
        {"id": "op-1", "run_id": "run", "action": "pause", "owner": "coordinator", "epoch": 1},
        {"id": "op-2", "run_id": "run", "action": "stop", "owner": "coordinator", "epoch": 1},
    ]
    service.store = SimpleNamespace(
        claim_control=lambda _owner: queue.pop(0) if queue else None,
        get_run=lambda _run_id: {"id": "run", "revision": 2},
        finish_control=lambda operation, error=None: calls.append((operation["id"], error)) or True,
        append_run_event=lambda _run_id, kind, payload: calls.append((kind, payload)),
    )
    service._control_unlocked = lambda _run_id, action, _revision: (_ for _ in ()).throw(
        RuntimeError("fixture failure")
    ) if action == "stop" else None
    assert service._process_controls() is True
    assert calls[0] == ("op-1", None)
    assert calls[2][0] == "op-2"
    assert "fixture failure" in calls[2][1]


def test_phase_metrics_record_last_and_max_duration():
    service = object.__new__(CompetitionService)
    service.phase_metrics = {}
    assert service._phase("demo", lambda: 42) == 42
    assert service.phase_metrics["demo"]["calls"] == 1
    assert service.phase_metrics["demo"]["last_seconds"] >= 0
    assert service.phase_metrics["demo"]["max_seconds"] >= 0


def test_degraded_summary_carries_prior_facts_and_artifacts():
    artifact = {"path": "/workspace/shared/amber/task/out", "sha256": "b" * 64}
    messages = [
        {"role": "user", "content": "task"},
        {"role": "user", "content": "[compacted context summary]\n" + json.dumps({
            "verified_facts": [{"claim": "port open", "evidence": "scan"}],
            "artifacts": [artifact],
        })},
        {"role": "assistant", "content": "old"},
    ]
    summary, _tail = deterministic_summary(messages, keep_recent=1)
    assert summary["verified_facts"][0]["claim"] == "port open"
    assert artifact in summary["artifacts"]


def test_deterministic_summary_handles_malformed_and_anthropic_tool_blocks():
    messages = [
        {"role": "user", "content": "task"},
        "not a message",
        {"role": "assistant", "content": [{"type": "tool_use", "name": "artifact_read"}]},
        {"role": "user", "content": "[compacted context summary]\nnot-json"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1"}]},
    ]
    summary, _tail = deterministic_summary(messages, keep_recent=1)
    assert "artifact_read" in summary["verified_facts"][0]["claim"]


def test_fit_history_bounds_structured_blocks_and_rejects_unshrinkable_message():
    history = [{"role": "user", "content": "task"}, {"role": "user", "content": [
        {"type": "text", "text": "x" * 100_000}, {"type": "tool_result", "content": "y" * 100_000},
        "plain",
    ]}]
    bounded = fit_history(history, max_bytes=4096)
    assert len(json.dumps(bounded).encode()) <= 4096
    try:
        fit_history([{"role": "user", "content": "x" * 1000, "meta": "z" * 1000}], max_bytes=1024)
    except ValueError as exc:
        assert "opening context" in str(exc)


def test_fit_history_bounds_single_oversized_opening_message():
    history = [{"role": "user", "content": "x" * 100_000}]
    bounded = fit_history(history, max_bytes=4096)
    assert len(json.dumps(bounded).encode()) <= 4096


def test_control_unlocked_handles_pause_resume_and_stop_branches():
    service = object.__new__(CompetitionService)
    events = []
    service.wake = lambda: events.append("wake")
    service._refresh_unlocked = lambda _run_id: events.append("refresh")
    service._stop_run_projects = lambda _run_id: events.append("projects")
    service._teardown_stopped_run = lambda *_args: events.append("teardown")
    service.store = SimpleNamespace(
        get_run=lambda _run_id: {"id": "run", "revision": 2, "status": "running"},
        transition_run=lambda *_args, **_kwargs: events.append("transition"),
        release_run_assignments=lambda _run_id: events.append("release"),
        append_run_event=lambda _run_id, kind, _payload: events.append(kind),
        run_snapshot=lambda _run_id: {"run": {"id": "run"}},
    )
    assert service._control_unlocked("run", "pause", 2) == {"run": {"id": "run"}}
    assert service._control_unlocked("run", "resume", 2) == {"run": {"id": "run"}}
    assert service._control_unlocked("run", "stop", 2) == {"run": {"id": "run"}}
    assert {"projects", "release", "teardown", "wake"}.issubset(events)


def test_control_operation_id_is_queued_even_when_lock_is_free():
    service = object.__new__(CompetitionService)
    import threading
    service._tick_lock = threading.Lock()
    service.wake = lambda: setattr(service, "woken", True)
    service.store = SimpleNamespace(
        queue_control=lambda run_id, action, revision, operation_id: {"id": operation_id, "action": action},
        run_snapshot=lambda _run_id: {"run": {"id": "run"}},
    )
    result = service.control("run", "refresh", 2, operation_id="op-2")
    assert result["control_operation"]["id"] == "op-2"
    assert service.woken is True


def test_control_worker_without_store_queue_is_a_noop():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace()
    assert service._process_controls() is False


def test_control_unlocked_rejects_stale_revision_and_unknown_action():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(get_run=lambda _run_id: {"revision": 3, "status": "running"})
    import pytest
    with pytest.raises(Exception, match="stale"):
        service._control_unlocked("run", "pause", 2)
    service.store = SimpleNamespace(get_run=lambda _run_id: {"revision": 2, "status": "running"})
    with pytest.raises(ValueError, match="unsupported"):
        service._control_unlocked("run", "unknown", 2)


def test_expired_cleanup_is_claimed_once_and_marked_done():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(orchestrator=None, db=None)
    service.store = SimpleNamespace(
        challenges=lambda _run_id: [{"id": "challenge", "state": "expired", "project_id": None}],
        append_run_event=lambda *_args, **_kwargs: None,
    )
    service._platform_for_run = lambda _run: object()
    service._instance_ready = lambda _challenge_id: False
    calls = []

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return SimpleNamespace(fetchone=lambda: {"id": "challenge"})

    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return Connection()
                def __exit__(self, *_args): return None
            return Context()

    service.state.db = DB()
    service._stop_expired_challenges({"id": "run"})
    assert len(calls) == 2
    assert "expiry_cleanup_state" in calls[0][0]
    assert "done" in str(calls[1][1])


def test_expired_cleanup_skips_a_claim_already_owned_by_another_tick():
    service = object.__new__(CompetitionService)
    service.state = SimpleNamespace(orchestrator=None, db=None)
    service.store = SimpleNamespace(
        challenges=lambda _run_id: [{"id": "challenge", "state": "expired", "project_id": None}],
        append_run_event=lambda *_args, **_kwargs: None,
    )
    service._platform_for_run = lambda _run: object()
    service._instance_ready = lambda _challenge_id: False

    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: None))
                def __exit__(self, *_args): return None
            return Context()

    service.state.db = DB()
    service._stop_expired_challenges({"id": "run"})


def test_instance_entry_publish_is_deduplicated_per_assignment_generation():
    service = object.__new__(CompetitionService)
    service._published_instance_entries = set()
    published = []
    service.publish_recon = lambda message: published.append(message)
    service.store = SimpleNamespace(
        assignments=lambda _run_id, active_only=True: [{
            "challenge_id": "challenge", "session_id": "session", "member": "amber",
            "role": "primary", "epoch": 2,
        }],
    )
    service.state = SimpleNamespace(db=None)

    class Connection:
        def execute(self, _sql, _params=()):
            return SimpleNamespace(fetchall=lambda: [{
                "challenge_id": "challenge", "external_id": "1244",
                "metadata": {"entry": "10.0.0.1:31337"}, "instance_generation": 3,
            }])

    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return Connection()
                def __exit__(self, *_args): return None
            return Context()

    service.state.db = DB()
    service._publish_instance_entries({"id": "run"})
    service._publish_instance_entries({"id": "run"})
    assert len(published) == 1
    assert published[0].payload["instance_generation"] == 3


def test_instance_entry_publish_skips_missing_entry_and_wp_and_records_publish_failure():
    service = object.__new__(CompetitionService)
    service._published_instance_entries = set()
    events = []
    service.publish_recon = lambda _message: (_ for _ in ()).throw(RuntimeError("bus down"))
    service.store = SimpleNamespace(
        assignments=lambda _run_id, active_only=True: [
            {"challenge_id": "missing", "session_id": "s1", "member": "amber", "role": "primary", "epoch": 1},
            {"challenge_id": "wp", "session_id": "s2", "member": "topaz", "role": "wp", "epoch": 1},
            {"challenge_id": "good", "session_id": "s3", "member": "agate", "role": "primary", "epoch": 1},
        ],
        append_run_event=lambda _run, kind, payload: events.append((kind, payload)),
    )
    service.state = SimpleNamespace(db=None)

    class DB:
        def connect(self):
            class Context:
                def __enter__(self):
                    return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchall=lambda: [
                        {"challenge_id": "missing", "external_id": "1", "metadata": {}, "instance_generation": 1},
                        {"challenge_id": "wp", "external_id": "2", "metadata": {"entry": "x"}, "instance_generation": 1},
                        {"challenge_id": "good", "external_id": "3", "metadata": {"entry": "x"}, "instance_generation": 1},
                    ]))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    service._publish_instance_entries({"id": "run"})
    assert events and events[-1][0] == "instance.entry_publish_failed"


def test_renewal_transient_retry_and_non_dict_success_paths():
    class DB:
        def connect(self):
            class Context:
                def __enter__(self):
                    return SimpleNamespace(execute=lambda sql, _params=(): SimpleNamespace(
                        fetchall=lambda: [{"challenge_id": "c", "external_id": "3"}] if "SELECT * FROM competition_instances" in sql else [],
                    ))
                def __exit__(self, *_args): return None
            return Context()

    state = SimpleNamespace(db=DB())
    service = object.__new__(CompetitionService)
    service.state = state
    service.store = SimpleNamespace(append_run_event=lambda *_args: None)
    class Failing:
        def renew_instance(self, _external_id):
            error = RuntimeError("temporary")
            error.retry_after = "bad"
            raise error
    service._platform_for_run = lambda _run: Failing()
    service._renew_instances({"id": "run"})

    class NonDict:
        def renew_instance(self, _external_id): return "renewed"
    service._platform_for_run = lambda _run: NonDict()
    service._renew_instances({"id": "run"})


def test_instance_lifecycle_without_platform_support_returns_false():
    service = object.__new__(CompetitionService)
    service._platform_for_run = lambda _run: SimpleNamespace(supports_instances=False)
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is False


def test_renewal_schedule_uses_explicit_margin():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    due = CompetitionService._instance_next_renewal({
        "expires_at": (now + timedelta(seconds=600)).isoformat(), "renew_before_seconds": 300,
    })
    assert 250 <= (due - now).total_seconds() <= 350


def test_expire_at_competition_end_updates_unfinished_challenges():
    service = object.__new__(CompetitionService)
    calls = []
    class DB:
        def connect(self):
            class Context:
                def __enter__(self):
                    return SimpleNamespace(execute=lambda sql, params=(): calls.append((sql, params)))
                def __exit__(self, *_args): return None
            return Context()
    service.state = SimpleNamespace(db=DB())
    service._expire_at_competition_end({"id": "run", "config_snapshot": {"workflow_spec": {"ends_at": "2000-01-01T00:00:00+00:00"}}})
    assert calls and "state='expired'" in calls[0][0]


def test_repair_assigned_expired_instance_starts_a_new_generation():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(
        assignments=lambda _run_id, active_only=True: [{"challenge_id": "c", "role": "primary"}],
        challenges=lambda _run_id: [{"id": "c", "state": "solving"}],
        append_run_event=lambda *_args: setattr(service, "repaired", True),
    )
    service.state = SimpleNamespace(db=None)
    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchall=lambda: [{"challenge_id": "c"}]))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    service._start_instance = lambda _run, _challenge: True
    service._repair_assigned_instances({"id": "run"})
    assert service.repaired is True


def test_start_instance_ready_result_persists_metadata_and_bumps_generation():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(append_run_event=lambda *_args: None)
    service.state = SimpleNamespace(db=None)
    class Platform:
        supports_instances = True
        def start_instance(self, _external_id): return {"state": "ready", "entry": "10.0.0.1:31337", "renew_after": 300}
    service._platform_for_run = lambda _run: Platform()
    calls = []
    class DB:
        def connect(self):
            class Context:
                def __enter__(self):
                    return SimpleNamespace(execute=lambda sql, params=(): (calls.append((sql, params)) or SimpleNamespace(fetchone=lambda: None)))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is True
    assert len(calls) == 3
    assert "instance_generation=instance_generation+1" in calls[2][0]


def test_start_instance_pending_result_is_recorded_without_admission():
    service = object.__new__(CompetitionService)
    events = []
    service.store = SimpleNamespace(append_run_event=lambda _run, kind, payload: events.append((kind, payload)))
    service.state = SimpleNamespace(db=None)
    class Platform:
        supports_instances = True
        def start_instance(self, _external_id): return {"state": "pending", "entry": ""}
    service._platform_for_run = lambda _run: Platform()
    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: None))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is False
    assert events[-1][0] == "instance.start_pending"


def test_start_instance_failure_records_expired_repair():
    service = object.__new__(CompetitionService)
    events = []
    service.store = SimpleNamespace(append_run_event=lambda _run, kind, payload: events.append((kind, payload)))
    service.state = SimpleNamespace(db=None)
    class Platform:
        supports_instances = True
        def start_instance(self, _external_id): raise RuntimeError("platform unavailable")
    service._platform_for_run = lambda _run: Platform()
    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: None))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is False
    assert events[-1][0] == "instance.start_failed"


def test_start_instance_respects_existing_ready_and_deferred_expired_records():
    service = object.__new__(CompetitionService)
    service.store = SimpleNamespace(append_run_event=lambda *_args: None)
    service.state = SimpleNamespace(db=None)
    class Platform:
        supports_instances = True
        def start_instance(self, _external_id): raise AssertionError("must not start")
    service._platform_for_run = lambda _run: Platform()
    class DB:
        def __init__(self, row): self.row = row
        def connect(self):
            row = self.row
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: row))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB({"state": "ready", "metadata": {}})
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is True
    service.state.db = DB({"state": "expired", "metadata": {"repair_after": "2099-01-01T00:00:00+00:00"}})
    assert service._start_instance({"id": "run"}, {"id": "c", "external_id": "3"}) is False


def test_run_completion_drains_terminal_challenges_and_finishes_when_empty():
    service = object.__new__(CompetitionService)
    events = []
    service.store = SimpleNamespace(
        challenges=lambda _run_id: [{"state": "solved"}],
        get_run=lambda _run_id: {"id": "run", "status": "running", "revision": 2},
        transition_run=lambda _run_id, status, **_kwargs: {"id": "run", "status": status, "revision": 3},
        release_run_assignments=lambda _run_id: events.append("release"),
        append_run_event=lambda _run_id, kind, _payload: events.append(kind),
    )
    service.state = SimpleNamespace(db=None)
    service._stop_run_projects = lambda _run_id: events.append("stop")
    class DB:
        def connect(self):
            class Context:
                def __enter__(self): return SimpleNamespace(execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: None))
                def __exit__(self, *_args): return None
            return Context()
    service.state.db = DB()
    service._advance_run_completion({"id": "run", "config_snapshot": {"workflow_spec": {"ends_at": ""}}})
    assert "run.draining" in events


def test_capacity_helpers_record_default_and_parse_mutable_limits():
    service = object.__new__(CompetitionService)
    service._instance_capability_warnings = set()
    events = []
    service.store = SimpleNamespace(append_run_event=lambda *args: events.append(args))
    service.state = SimpleNamespace(config=SimpleNamespace(limits=SimpleNamespace(max_concurrent_tasks=7)))
    platform = SimpleNamespace(supports_instances=True, instance_limit=lambda: None, start_instance=lambda _id: {})
    run = {"id": "run", "limits": '{"max_concurrent_tasks": "bad"}', "config_snapshot": {"workflow_spec": {}}}
    assert service._remote_instance_limit(run, platform) == 1
    assert service._remote_instance_limit(run, platform) == 1
    assert events and events[0][1] == "instance.limit_defaulted"
    assert service._local_task_limit(run) == 7
    assert service._local_task_limit({"limits": {"max_concurrent_tasks": 3}}) == 3


def test_renewal_helper_ignores_malformed_expiry_and_uses_default():
    service = object.__new__(CompetitionService)
    before = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    result = service._instance_next_renewal({"expires_at": "not-a-date"}, default_seconds=17)
    assert 10 <= (result - before).total_seconds() <= 25
    assert service._resolve_remote_limit({"remote_instance_limit": 2}, SimpleNamespace(supports_instances=False)) == (2, "workflow.remote_instance_limit")
    from datetime import datetime, timezone
    value = datetime.now(timezone.utc)
    assert CompetitionService._instance_next_renewal({"expires_at": value}).tzinfo is not None


def test_member_message_job_wait_and_artifact_read_actions_are_bounded():
    import asyncio

    from backend.members.adapters import MemberAction
    from backend.members.base_member import BaseMember

    class Logger:
        def project(self, *_args, **_kwargs): pass
        def tool(self, *_args, **_kwargs): pass
        def memory(self, *_args, **_kwargs): pass

    sandbox = SimpleNamespace(read_page=lambda path, **kwargs: {"path": path, **kwargs})
    deps = SimpleNamespace(
        sandbox=sandbox, logger=Logger(), db=None, memory=None,
        registry=SimpleNamespace(search=lambda _query, **_kwargs: []),
        mcps=SimpleNamespace(names=list), container_mcps={},
        runtime=SimpleNamespace(tool_output_bytes=4096), on_message=lambda *args: {"ok": True},
        on_fact=None, lease_owner=None, lease_token=None, competition_store=None,
    )
    member = BaseMember("amber", SimpleNamespace(), deps)
    member._jobs = lambda: SimpleNamespace(wait=lambda job_id, **kwargs: {"job_id": job_id, "state": "exited"})
    async def dispatch(action):
        return await member._dispatch("project", "intent", "misc", action, 1, SimpleNamespace(), allow_intent=True)
    message = asyncio.run(dispatch(MemberAction.from_obj({"action": "message", "receiver": "jade", "content": "check artifact"})))
    assert message.graph_action == "message"
    waited = asyncio.run(dispatch(MemberAction.from_obj({"action": "job_wait", "job_id": "job1", "timeout": 2})))
    assert "exited" in waited.observation
    page = asyncio.run(dispatch(MemberAction.from_obj({"action": "artifact_read", "path": "/workspace/shared/a", "offset": 0})))
    assert "workspace/shared/a" in page.observation
    invalid = asyncio.run(dispatch(MemberAction.from_obj({"action": "message", "receiver": "amber", "content": "no"})))
    assert invalid.invalid_action is True
    member._jobs = lambda: None
    unavailable = asyncio.run(dispatch(MemberAction.from_obj({"action": "job_wait", "job_id": "job1"})))
    assert unavailable.invalid_action is True
    bad_fact = asyncio.run(dispatch(MemberAction.from_obj({"action": "fact", "description": "", "evidence": ""})))
    assert bad_fact.invalid_action is True
    bad_artifact = asyncio.run(dispatch(MemberAction.from_obj({
        "action": "fact", "description": "claim", "evidence": "evidence",
        "artifacts": [{"path": "/tmp/out", "sha256": "bad"}],
    })))
    assert bad_artifact.invalid_action is True
    bad_install = asyncio.run(dispatch(MemberAction.from_obj({"action": "install", "manager": "unknown", "packages": ["x"]})))
    assert bad_install.invalid_action is True
    searched = asyncio.run(dispatch(MemberAction.from_obj({"action": "tool_search", "query": "reverse"})))
    assert "tool_search:reverse" in searched.observation
    bad_tool = asyncio.run(dispatch(MemberAction.from_obj({"action": "tool", "server": "", "tool": ""})))
    assert bad_tool.invalid_action is True
    class MCP:
        async def call_tool(self, *_args): raise RuntimeError("mcp unavailable")
    mcp_error = asyncio.run(member._dispatch(
        "project", "intent", "misc", MemberAction.from_obj({"action": "tool", "server": "browser", "tool": "fetch", "args": {}}),
        1, MCP(), allow_intent=True,
    ))
    assert "mcp unavailable" in mcp_error.observation
    member._probe_cli_tools = lambda: {"readelf": True, "ghidra": False}
    note = member._runtime_tool_inventory_note()
    assert "available now: readelf" in note
    assert "missing now: ghidra" in note
    member.deps.sandbox = SimpleNamespace()
    no_reader = asyncio.run(dispatch(MemberAction.from_obj({"action": "artifact_read", "path": "/workspace/shared/a"})))
    assert no_reader.invalid_action is True
