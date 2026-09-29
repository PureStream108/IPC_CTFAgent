"""IPC action-agent behaviour that needs no database.

Covers the Workflow-panel selection contract (a selected platform is described
to IPC so it asks only for the remaining personalisation) and the containment
guard on the task-sandbox tool.
"""

from __future__ import annotations

import pytest

from backend.ops.service import OpsAgentService
from backend.ops.tools import OpsToolError, OpsToolExecutor


class _Store:
    def __init__(self, workflow=None):
        self._workflow = workflow

    def get_workflow(self, workflow_id):
        if self._workflow is None or workflow_id != "wf_selected":
            raise KeyError(workflow_id)
        return self._workflow


def _service(workflow=None) -> OpsAgentService:
    service = object.__new__(OpsAgentService)
    service.store = _Store(workflow)
    return service


_WORKFLOW = {
    "name": "ISCTF2025 game 32",
    "status": "confirmed",
    "spec": {
        "challenges": {"list_url": "https://example.test/api/Game/32/Challenges"},
        "submit": {"method": "POST", "url": "https://example.test/api/submit"},
    },
}


@pytest.mark.parametrize("workflow_id", [None, ""])
def test_no_selected_workflow_means_adapt_a_new_platform(workflow_id):
    assert _service(_WORKFLOW)._workflow_context(workflow_id) == ""


def test_unknown_workflow_is_ignored_rather_than_failing_the_turn():
    assert _service(_WORKFLOW)._workflow_context("wf_missing") == ""


def test_selected_workflow_is_described_and_asks_only_for_the_remainder():
    context = _service(_WORKFLOW)._workflow_context("wf_selected")
    assert "SELECTED PLATFORM WORKFLOW" in context
    assert "wf_selected" in context
    assert "ISCTF2025 game 32" in context
    assert "https://example.test/api/Game/32/Challenges" in context
    assert "POST https://example.test/api/submit" in context
    assert "Build on this existing configuration" in context
    assert "ipc_question" in context


def test_selected_workflow_without_a_submit_endpoint_still_renders():
    workflow = {"name": "list only", "status": "draft", "spec": {"challenges": {}}}
    context = _service(workflow)._workflow_context("wf_selected")
    assert "list only" in context
    assert "submit:" not in context


@pytest.mark.parametrize(
    "command",
    [
        "cat /workspace/shared/notes.txt",
        "python3 -c 'print(1)'",
        "ls /tmp",
    ],
)
def test_task_sandbox_exec_allows_container_commands(monkeypatch, command):
    executor = object.__new__(OpsToolExecutor)
    attached = {}

    def fake_sandbox(project_id):
        attached["project_id"] = project_id
        raise OpsToolError("stop after the policy check")

    monkeypatch.setattr(executor, "_task_sandbox", fake_sandbox, raising=False)
    with pytest.raises(OpsToolError, match="stop after the policy check"):
        executor.task_sandbox_exec("proj_001", command, 30)
    assert attached["project_id"] == "proj_001"


@pytest.mark.parametrize(
    "command",
    [
        "cat D:/Desktop/IPC_CTFAgent/.qa-artifacts/run/ISCTF2025-WriteUp.pdf",
        "/d/Language/Python314/python solve.py",
        "chroot /host /bin/bash -lc id",
    ],
)
def test_task_sandbox_exec_refuses_host_paths(monkeypatch, command):
    executor = object.__new__(OpsToolExecutor)

    def fail(project_id):
        raise AssertionError("the sandbox must not be attached for a blocked command")

    monkeypatch.setattr(executor, "_task_sandbox", fail, raising=False)
    with pytest.raises(OpsToolError, match="stay inside the container"):
        executor.task_sandbox_exec("proj_001", command, 30)
