from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from backend.agent.compaction import fit_history, summarize
from backend.sandbox.sandbox import LocalSandbox


def test_forty_megabyte_output_is_spooled_and_fully_readable(tmp_path):
    sandbox = LocalSandbox("output-budget", tmp_path)
    result = sandbox.exec(f'"{sys.executable}" -c "import sys;sys.stdout.buffer.write(b\'x\'*(40*1024*1024))"')
    assert result.ok
    assert len(result.stdout.encode()) < 16384
    artifact = result.artifacts["stdout"]
    assert artifact["bytes"] == 40 * 1024 * 1024
    path = Path(artifact["path"])
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == artifact["sha256"]
    first = sandbox.read_page(str(path), limit=8192)
    last = sandbox.read_page(str(path), offset=artifact["bytes"]-8, limit=8)
    assert first["data"] == "x" * 8192
    assert first["next_offset"] == 8192
    assert last["data"] == "x" * 8
    assert last["next_offset"] == artifact["bytes"]
    with pytest.raises(ValueError):
        sandbox.read_page("../../outside")


def test_large_recent_result_cannot_survive_the_context_budget():
    history = [{"role": "user", "content": "Investigate the attachment"},
               {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "x" * (40 * 1024 * 1024)}]
    projection = fit_history(history, max_bytes=16384, max_tokens=4000)
    assert len(json.dumps(projection).encode()) <= 16384
    assert projection[0]["content"] == history[0]["content"]
    assert all(item.get("role") != "tool" or any(
        call.get("id") == item.get("tool_call_id") for message in projection
        for call in message.get("tool_calls", [])
    ) for item in projection)


def test_compaction_failure_reports_its_reason_and_preserves_artifact_refs():
    reasons = []
    artifact = {"path": "/workspace/shared/result.log", "sha256": "a" * 64}
    history = [{"role": "user", "content": "task"}, {"role": "tool", "content": json.dumps({"process": {"artifacts": {"stdout": artifact}}})},
               *[{"role": "user", "content": str(i)} for i in range(20)]]
    def unavailable(_prompt, _history):
        raise TimeoutError("summary deadline")
    summary, _tail = summarize(history, request=unavailable, on_degraded=reasons.append)
    assert reasons == ["TimeoutError: summary deadline"]
    assert artifact in summary["artifacts"]
