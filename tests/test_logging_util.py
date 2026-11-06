from __future__ import annotations

import json
import threading

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


def test_logger_accepts_kind_as_record_field(tmp_path):
    logger = IPCLogger(tmp_path)

    logger.llm("ops_agent_event", "proj_001", kind="tool", label="Bash")

    entry = logger.read_log("llm", "proj_001")[-1]
    assert entry["event"] == "ops_agent_event"
    assert entry["kind"] == "tool"
    assert entry["label"] == "Bash"


def test_file_stats_counts_every_entry_beyond_the_page_size(tmp_path):
    logger = IPCLogger(tmp_path)
    for index in range(501):
        logger.project("tick", "proj_1", index=index)
    path = tmp_path / "project_logs" / "proj_1.jsonl"
    assert logger.count_file(path) == 501
    assert len(logger.read_project_log("proj_1", 500)) == 500
    assert logger.count_file(path) == 501


def test_file_stats_rescans_only_appended_bytes(tmp_path):
    logger = IPCLogger(tmp_path)
    logger.project("first", "proj_2")
    path = tmp_path / "project_logs" / "proj_2.jsonl"
    first = logger.file_stats(path)
    assert first["entry_count"] == 1
    assert first["scan_offset"] == path.stat().st_size

    logger.project("second", "proj_2")
    second = logger.file_stats(path)
    assert second["entry_count"] == 2
    assert second["file_id"] == first["file_id"]
    assert second["scan_offset"] > first["scan_offset"]


def test_file_stats_ignores_half_written_lines(tmp_path):
    logger = IPCLogger(tmp_path)
    path = tmp_path / "project_logs" / "proj_3.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"event": "complete"}\n{"event": "half')
    assert logger.count_file(path) == 1

    with path.open("ab") as handle:
        handle.write(b'"}\n')
    assert logger.count_file(path) == 2


def test_file_stats_recounts_when_the_file_is_replaced(tmp_path):
    logger = IPCLogger(tmp_path)
    path = tmp_path / "project_logs" / "proj_4.jsonl"
    first = tmp_path / "project_logs" / "tmp_first.jsonl"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text('{"event": "a"}\n{"event": "b"}\n{"event": "c"}\n', encoding="utf-8")
    path.write_bytes(first.read_bytes())
    assert logger.count_file(path) == 3

    first.unlink()
    first.write_text('{"event": "a"}\n', encoding="utf-8")
    path.unlink()
    path.write_bytes(first.read_bytes())
    assert logger.count_file(path) == 1


def test_file_stats_reads_legacy_json_arrays(tmp_path):
    logger = IPCLogger(tmp_path)
    path = tmp_path / "project_logs" / "proj_5.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[{"event": "a"}, {"event": "b"}]', encoding="utf-8")
    assert logger.count_file(path) == 2
    assert logger.count_file(tmp_path / "project_logs" / "missing.json") == 0


def test_concurrent_appends_keep_every_line(tmp_path):
    logger = IPCLogger(tmp_path)
    workers = 6
    per_worker = 40
    barrier = threading.Barrier(workers)

    def run(worker: int) -> None:
        barrier.wait()
        for index in range(per_worker):
            logger.project("tick", "proj_6", worker=worker, index=index)

    threads = [threading.Thread(target=run, args=(worker,)) for worker in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    path = tmp_path / "project_logs" / "proj_6.jsonl"
    assert logger.count_file(path) == workers * per_worker
    assert len(logger.read_project_log("proj_6", None)) == workers * per_worker
