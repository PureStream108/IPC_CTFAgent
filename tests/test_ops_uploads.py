from __future__ import annotations

import io
from pathlib import Path

from backend.ops.attachments import OpsAttachmentStore
from backend.ops.tools import OpsToolExecutor


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Service:
    def __init__(self, root: Path) -> None:
        self.store = _Store(root)


class _Sandbox:
    def __init__(self) -> None:
        self.published: list[Path] = []

    def publish(self, paths) -> int:
        self.published.extend(Path(p) for p in paths)
        return len(self.published)


class _State:
    def __init__(self, root: Path, projects: Path) -> None:
        self.ops_agent_service = _Service(root)
        self._projects = projects

    def attachments_dir(self, project_id: str) -> Path:
        target = self._projects / project_id / "attachments"
        target.mkdir(parents=True, exist_ok=True)
        return target


def test_prompt_context_names_the_publish_tool_for_binary_uploads(tmp_path):
    store = OpsAttachmentStore(tmp_path)
    saved = store.save(io.BytesIO(b"PK\x03\x04handout"), filename="gitq_handout.zip")

    context = store.prompt_context([str(saved["id"])])

    assert saved["id"] in context
    assert "gitq_handout.zip" in context
    assert "publish_upload" in context
    assert "ATTACHED DOCUMENT" in context


def test_publish_upload_writes_the_project_copy_and_the_sandbox(tmp_path):
    root = tmp_path / "ops-agent"
    store = OpsAttachmentStore(root)
    saved = store.save(io.BytesIO(b"PK\x03\x04handout"), filename="gitq_handout.zip")
    state = _State(root, tmp_path / "projects")
    executor = OpsToolExecutor(state)
    sandbox = _Sandbox()
    executor._task_sandbox = lambda project_id: sandbox

    result = executor.publish_upload(str(saved["id"]), "proj_753")

    assert result["sandbox_path"] == "/workspace/attachments/gitq_handout.zip"
    assert result["copied_into_container"] is True
    assert result["bytes"] == len(b"PK\x03\x04handout")
    project_copy = tmp_path / "projects" / "proj_753" / "attachments" / "gitq_handout.zip"
    assert project_copy.read_bytes() == b"PK\x03\x04handout"
    assert [path.name for path in sandbox.published] == ["gitq_handout.zip"]


def test_publish_upload_rejects_an_unknown_id(tmp_path):
    executor = OpsToolExecutor(_State(tmp_path / "ops-agent", tmp_path / "projects"))
    executor._task_sandbox = lambda project_id: _Sandbox()

    try:
        executor.publish_upload("upload_" + "0" * 24, "proj_753")
    except Exception as exc:  # noqa: BLE001 - OpsToolError is the expected failure
        assert "unknown upload" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown upload id must fail")
