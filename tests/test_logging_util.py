from __future__ import annotations

import json

from backend.core.logging_util import IPCLogger


def test_logger_appends_jsonl_records(tmp_path):
    logger = IPCLogger(tmp_path)
    logger.project("created", "proj_001", title="Demo")
    path = tmp_path / "project_logs" / "proj_001.jsonl"
    first = path.read_text(encoding="utf-8")

    logger.project("updated", "proj_001", status="running")
    lines = path.read_text(encoding="utf-8").splitlines()

    assert first == lines[0] + "\n"
    assert [json.loads(line)["event"] for line in lines] == ["created", "updated"]
    assert logger.read_project_log("proj_001", None)[-1]["status"] == "running"


def test_logger_reads_legacy_json_array(tmp_path):
    path = tmp_path / "project_logs" / "legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text('[{"event":"old","project_id":"legacy"}]\n', encoding="utf-8")

    logger = IPCLogger(tmp_path, project_filename_resolver=lambda project_id: "legacy.json")

    assert logger.read_project_log("legacy") == [{"event": "old", "project_id": "legacy"}]
