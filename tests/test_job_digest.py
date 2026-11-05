from __future__ import annotations

from backend.agent.job_digest import (
    _clip,
    _line_hits,
    _unique,
    build_deterministic_digest,
    format_job_digest,
    merge_curator_digest,
    parse_curator_response,
)


def test_deterministic_digest_keeps_flag_and_redacts_credentials():
    job = {"id": "job-1", "member": "amber", "command": "curl ...", "state": "exited", "exit_code": 0}
    stdout = "HTTP/1.1 200 OK\nflag{demo-flag}\npassword=hunter2\n"
    digest = build_deterministic_digest(job, stdout, "")

    assert digest["verdict"] == "success"
    assert any(item["type"] == "flag" and item["value"] == "flag{demo-flag}" for item in digest["key_findings"])
    assert any(item["type"] == "credential" and "hunter2" not in item["value"] for item in digest["key_findings"])
    assert "hunter2" not in digest["excerpt"]
    assert digest["source"] == "deterministic"


def test_deterministic_digest_marks_lost_as_unknown():
    digest = build_deterministic_digest(
        {"id": "job-2", "member": "amber", "command": "sleep", "state": "lost", "exit_code": None}
    )
    assert digest["verdict"] == "unknown"
    assert "不要" in digest["summary_zh"]


def test_curator_json_is_parsed_and_merged():
    raw = 'Here is the result:\n```json\n{"summary_zh":"发现了一个入口","verdict":"success","key_findings":[{"type":"endpoint","value":"http://target/","confidence":0.9}],"suggested_next":["验证入口"]}\n```'
    parsed = parse_curator_response(raw)
    assert parsed is not None
    deterministic = build_deterministic_digest(
        {"id": "job-3", "member": "amber", "command": "curl", "state": "exited", "exit_code": 0},
        "flag{demo}",
    )
    merged = merge_curator_digest(deterministic, parsed)
    assert merged["source"] == "sugilite"
    assert merged["summary_zh"] == "发现了一个入口"
    assert any(item["type"] == "flag" and item["value"] == "flag{demo}" for item in merged["key_findings"])
    assert any(item["value"] == "http://target/" for item in merged["key_findings"])


def test_format_job_digest_contains_job_and_artifact_hint():
    digest = build_deterministic_digest(
        {"id": "job-4", "member": "amber", "command": "nmap -p- target", "state": "exited", "exit_code": 0},
        "22/tcp open ssh",
    )
    text = format_job_digest({"id": "job-4", "member": "amber", "command": "nmap -p- target", "state": "exited", "exit_code": 0, "digest_json": digest})
    assert "Sugilite" in text
    assert "job_logs job_id=job-4" in text
    assert "nmap" in text


def test_digest_clipping_deduplication_and_limits():
    assert _clip("x" * 20, 8).endswith("…")
    assert _unique(["a", "a", "b"], 2) == ["a", "b"]
    hits = _line_hits("\n".join(f"error {i}" for i in range(20)), __import__("re").compile("error"), limit=3)
    assert len(hits) == 3
    duplicate = _line_hits("error same\nerror same\n", __import__("re").compile("error"))
    assert duplicate == [(1, "error same")]


def test_deterministic_status_and_finding_branches():
    cancelled = build_deterministic_digest(
        {"id": "j1", "state": "cancelled", "exit_code": None},
        "http://target/ 10.0.0.1:8080 HTTP/1.1 500 error: denied",
        output_truncated=True,
        error="curator boom",
    )
    assert cancelled["verdict"] == "cancelled"
    assert cancelled["output_truncated"] is True
    assert cancelled["needs_raw_output"] is True
    assert "curator boom" in cancelled["summary_zh"]
    assert any(item["type"] == "endpoint" and item["value"] == "http://target/" for item in cancelled["key_findings"])
    assert any(item["type"] == "endpoint" and item["value"] == "10.0.0.1:8080" for item in cancelled["key_findings"])
    assert any(item["type"] == "error" for item in cancelled["key_findings"])
    assert any("job_logs" in item for item in cancelled["suggested_next"])

    timeout = build_deterministic_digest({"id": "j2", "state": "exited", "exit_code": 124})
    assert timeout["verdict"] == "unknown"
    assert any("job_status" in item for item in timeout["suggested_next"])
    failed = build_deterministic_digest({"id": "j3", "state": "exited", "exit_code": 9})
    assert failed["verdict"] == "failed"

    long_text = "A" * 3000 + "\npassword=secret"
    clipped = build_deterministic_digest({"id": "j4", "state": "exited", "exit_code": 0}, long_text)
    assert "[excerpt clipped]" in clipped["excerpt"]
    assert "secret" not in clipped["excerpt"]
    empty = build_deterministic_digest({"id": "j5", "state": "exited", "exit_code": 0})
    assert empty["excerpt"] == "（无 stdout/stderr 输出）"


def test_parse_curator_response_accepts_and_rejects_boundaries():
    assert parse_curator_response("") is None
    assert parse_curator_response("no json here") is None
    assert parse_curator_response("{not json}") is None
    assert parse_curator_response("[1, 2]") is None
    assert parse_curator_response('[{"a": 1}]') is None
    assert parse_curator_response('{"other": 1}') is None
    assert parse_curator_response('{"summary_zh": "ok", "key_findings": {}}')["key_findings"] == []
    assert parse_curator_response('{"summary_zh": "ok", "key_findings": [1, {"type": "x"}, {"type": "ok", "value": "v"}]}')["key_findings"] == [
        {"type": "ok", "value": "v", "evidence": "", "confidence": None}
    ]

    fenced = 'prefix\n```json\n{"summary_zh": "s", "key_findings": [{"type": "x", "value": "v"}], "useful_commands": ["a", ""], "suggested_next": ["b"], "risk_flags": ["r"], "needs_raw_output": 1}\n```'
    parsed = parse_curator_response(fenced)
    assert parse_curator_response('```json\n{"summary_zh": "fenced"}\n```')["summary_zh"] == "fenced"
    assert parse_curator_response('```json\n{"summary_zh": "unclosed"}')["summary_zh"] == "unclosed"
    assert parsed["summary_zh"] == "s"
    assert parsed["useful_commands"] == ["a"]
    assert parsed["suggested_next"] == ["b"]
    assert parsed["risk_flags"] == ["r"]
    assert parsed["needs_raw_output"] is True


def test_merge_drops_invalid_and_duplicate_findings_and_formats_non_dicts():
    deterministic = build_deterministic_digest(
        {"id": "j", "state": "exited", "exit_code": 0}, "flag{x} http://a/"
    )
    assert merge_curator_digest(deterministic, None) is deterministic
    merged = merge_curator_digest(
        deterministic,
        {
            "summary_zh": "curated",
            "key_findings": [
                1,
                {"type": "flag", "value": "flag{x}"},
                {"type": "endpoint", "value": "http://b/"},
                {"type": "endpoint", "value": "http://b/"},
            ],
            "useful_commands": ["run"],
            "risk_flags": ["review"],
        },
    )
    assert merged["summary_zh"] == "curated"
    assert sum(1 for item in merged["key_findings"] if item.get("value") == "http://b/") == 1
    assert merged["risk_flags"] == ["review"]

    text = format_job_digest({"digest_json": {**deterministic, "key_findings": [1, {"type": "x", "value": "v"}]}, "id": "j", "command": "cmd"})
    assert "cmd" in text and "[x] v" in text
    assert format_job_digest({"id": "j"}, max_chars=5).endswith("…")
