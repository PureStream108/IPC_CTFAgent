from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ops.agent_bridge import OpsToolBridge
from backend.ops.presets import preset_setup_prompt, workflow_preset, workflow_templates
from backend.ops.tools import OpsToolError, OpsToolExecutor, tool_definitions


def test_builtin_gzctf_tsecbench_and_ret2shell_presets_are_registered():
    presets = {item["id"]: item for item in workflow_templates()}
    assert set(presets) == {"gzctf", "tsecbench", "ret2shell", "ichunqiu", "nestctf"}
    assert presets["gzctf"]["default"] is True
    assert presets["gzctf"]["ipc_setup"]["tool"] == "gzctf_workflow"
    assert presets["tsecbench"]["ipc_setup"]["tool"] == "tsecbench_workflow"
    assert presets["ret2shell"]["ipc_setup"]["tool"] == "ret2shell_workflow"
    assert [field["key"] for field in presets["gzctf"]["ipc_setup"]["personalization"]][:2] == [
        "base_url",
        "game_id",
    ]
    assert [field["key"] for field in presets["ret2shell"]["ipc_setup"]["personalization"]][:2] == [
        "base_url",
        "game_id",
    ]
    assert "gzctf_workflow" in preset_setup_prompt("gzctf")
    assert "benchmark_token" in preset_setup_prompt("tsecbench")
    assert "ret2shell_workflow" in preset_setup_prompt("ret2shell")
    assert "ret2shell_token" in preset_setup_prompt("ret2shell")
    assert workflow_preset("missing") is None

    from backend.platform.gzctf.workflow import GZCTFWorkflowInput

    spec = GZCTFWorkflowInput(
        base_url="https://ctf.example",
        game_id=7,
        category_map={"*": "pentest"},
    ).workflow_spec()
    assert spec.challenges.category_map == {"*": "pentest"}


def test_ipc_catalog_exposes_all_native_preset_tools():
    names = {item["name"] for item in tool_definitions()}
    assert {"gzctf_workflow", "tsecbench_workflow", "ret2shell_workflow"} <= names
    assert {"platform_preflight", "platform_start", "platform_status"} <= names
    descriptions = {item["name"]: item["description"] for item in tool_definitions()}
    assert "propose_platform_workflow" in descriptions["gzctf_workflow"]
    assert "secret_name=gzctf_token" in descriptions["gzctf_workflow"]
    assert "secret_name=benchmark_token" in descriptions["tsecbench_workflow"]
    assert "secret_name=ret2shell_token" in descriptions["ret2shell_workflow"]
    assert "sandbox" in descriptions["platform_start"]
    assert "never require" in descriptions["platform_start"]


class _PresetService:
    def __init__(self):
        self.calls: list[tuple[str, dict, str | None]] = []

    def configure_preset_workflow(self, template_id, settings, workflow_id=None):
        self.calls.append((template_id, settings, workflow_id))
        return {
            "id": f"wf_{template_id}",
            "name": template_id,
            "status": "draft",
        }

    def _run_ops_tool(self, name, arguments, session_id):
        assert name == "gzctf_workflow"
        return self.configure_preset_workflow("gzctf", arguments["settings"])


def test_ipc_preset_tools_use_the_native_configurers():
    service = _PresetService()
    executor = OpsToolExecutor(SimpleNamespace(ops_agent_service=service))
    assert executor.execute(
        "gzctf_workflow",
        {"settings": {"base_url": "https://ctf.example", "game_id": 7}},
    )["id"] == "wf_gzctf"
    assert executor.execute(
        "tsecbench_workflow",
        {"settings": {"base_url": "https://bench.example"}},
    )["id"] == "wf_tsecbench"
    assert executor.execute(
        "ret2shell_workflow",
        {"settings": {"base_url": "https://r2s.example", "game_id": 12}},
    )["id"] == "wf_ret2shell"
    assert service.calls == [
        ("gzctf", {"base_url": "https://ctf.example", "game_id": 7}, None),
        ("tsecbench", {"base_url": "https://bench.example"}, None),
        ("ret2shell", {"base_url": "https://r2s.example", "game_id": 12}, None),
    ]


def test_ipc_preset_tools_refuse_credentials_in_ordinary_settings():
    executor = OpsToolExecutor(SimpleNamespace(ops_agent_service=_PresetService()))
    with pytest.raises(OpsToolError, match="credentials must use question"):
        executor.execute(
            "gzctf_workflow",
            {"settings": {"base_url": "https://ctf.example", "game_id": 7, "secrets": {"gzctf_token": "x"}}},
        )


def test_created_preset_is_returned_to_the_ui_as_a_workflow():
    service = _PresetService()
    bridge = OpsToolBridge(service, "ops_0123456789abcdef")
    result = bridge.execute(
        "gzctf_workflow",
        {"settings": {"base_url": "https://ctf.example", "game_id": 7}},
        idempotency_key="fixture",
    )
    assert result["id"] == "wf_gzctf"
    assert [item["id"] for item in bridge.proposals] == ["wf_gzctf"]
    assert bridge.tool_events == [
        {"name": "gzctf_workflow", "project_id": None, "ok": True}
    ]


class _RunStore:
    def run_snapshot(self, run_id):
        return {"run": {"id": run_id, "status": "running"}}

    def member_snapshot(self, run_id):
        return [{"member": "amber", "state": "solving", "run_id": run_id}]


class _Competition:
    def __init__(self):
        self.store = _RunStore()
        self.calls: list[tuple] = []

    def preflight(self, workflow_id):
        self.calls.append(("preflight", workflow_id))
        return {
            "workflow_id": workflow_id,
            "challenge_count": 2,
            "challenges": [
                {"external_id": "done", "solved": True},
                {"external_id": "todo", "solved": False},
            ],
        }

    def start(self, workflow_id, idempotency_key, select=None):
        self.calls.append(("start", workflow_id, idempotency_key, select))
        return {
            "run": {"id": "run_1", "status": "running"},
            "capacity": {"solving": 1, "idle": 9},
        }


def test_api_exposes_generic_preset_routes():
    from backend.api.ops_agent import router

    routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in router.routes
        if hasattr(route, "methods")
    }
    assert ("/api/ops/workflows/{template_id}", ("POST",)) in routes
    assert ("/api/ops/workflows/{workflow_id}/{template_id}", ("PUT",)) in routes


def test_system_prompt_lists_every_registry_preset():
    from backend.ops.service import _SYSTEM_PROMPT

    for template in workflow_templates():
        assert template["ipc_setup"]["tool"] in _SYSTEM_PROMPT
        for secret_name in template["secret_names"]:
            assert secret_name in _SYSTEM_PROMPT


def test_ops_service_configures_the_ret2shell_preset():
    from backend.ops.service import OpsAgentService

    class _Store:
        def __init__(self):
            self.created = None

        def create_workflow(self, spec, *, source, secrets_values):
            self.created = (spec, source, secrets_values)
            return {
                "id": "wf_r2s",
                "spec": spec,
                "status": "draft",
                "spec_digest": "digest",
                "name": spec.name,
                "session_id": None,
                "source": source,
                "created_at": "",
                "updated_at": "",
            }

        def workflow_secrets(self, _workflow_id):
            return {"ret2shell_token": "tok"}

    service = object.__new__(OpsAgentService)
    service.store = _Store()
    view = service.configure_preset_workflow(
        "ret2shell",
        {
            "base_url": "https://r2s.example",
            "game_id": 12,
            "secrets": {"ret2shell_token": "tok"},
        },
    )
    spec, source, secrets_values = service.store.created
    assert spec.template_id == "ret2shell"
    assert spec.challenges.platform == "ret2shell"
    assert spec.challenges.game_id == 12
    assert source == "manual"
    assert secrets_values == {"ret2shell_token": "tok"}
    assert view["id"] == "wf_r2s"
    assert view["spec"]["challenges"]["game_id"] == 12
    assert view["spec"]["challenges"]["platform"] == "ret2shell"


def test_ipc_platform_tools_start_a_run_without_a_sandbox():
    competition = _Competition()
    executor = OpsToolExecutor(SimpleNamespace(competition=competition))

    preflight = executor.execute("platform_preflight", {"workflow_id": "wf_1"})
    assert preflight["challenge_count"] == 2
    assert preflight["challenges"][0]["solved"] is True

    started = executor.execute(
        "platform_start",
        {"workflow_id": "wf_1", "idempotency_key": "ipc-fixture-1"},
    )
    assert started["run"]["id"] == "run_1"
    assert started["capacity"]["solving"] == 1

    status = executor.execute("platform_status", {"run_id": "run_1"})
    assert status["run"]["run"]["status"] == "running"
    assert status["members"][0]["state"] == "solving"

    assert competition.calls == [
        ("preflight", "wf_1"),
        ("start", "wf_1", "ipc-fixture-1", None),
    ]


def test_ipc_platform_start_requires_a_usable_idempotency_key():
    competition = _Competition()
    executor = OpsToolExecutor(SimpleNamespace(competition=competition))
    with pytest.raises(OpsToolError, match="at least 8"):
        executor.execute(
            "platform_start",
            {"workflow_id": "wf_1", "idempotency_key": "short"},
        )
    assert competition.calls == []
