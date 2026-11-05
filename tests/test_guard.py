from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.pentest import guard, overlay, state_doc
from backend.pentest.diamond_policy import Board, Caps, ProjectCard, Seat


def test_platform_denylist_hits():
    hits = guard.scan("nmap -p- 172.19.0.1")
    assert [hit["kind"] for hit in hits] == ["platform"]
    assert hits[0]["match"] == "172.19.0.1"


def test_project_denylist_extends_the_defaults():
    hits = guard.scan("curl http://10.9.9.9/internal/db", denylist=["10.9.9.9"])
    assert hits and hits[0]["kind"] == "platform"
    assert "172.19.0.1" in guard.merge_denylist([])


def test_token_guessing_is_a_platform_violation():
    hits = guard.scan('curl -H "X-Benchmark-Token: abc" https://target.test')
    assert [hit["kind"] for hit in hits] == ["platform"]


def test_answer_searching_is_detected_but_cve_lookup_is_not():
    assert guard.scan("google: A-03 题解 writeup")[0]["kind"] == "answers"
    assert guard.scan("search CVE-2023-22515 official docs") == []
    assert guard.scan("curl https://nvd.nist.gov/vuln/detail/CVE-2023-22515") == []


def test_destructive_commands_are_detected():
    for command in ("rm -rf /", "mkfs.ext4 /dev/sda1", "shutdown -h now", "drop database x"):
        hits = guard.scan(command)
        assert hits and hits[0]["kind"] == "destructive", command


def test_normal_targeting_is_clean():
    assert guard.scan("curl -s http://10.0.3.15:8080/admin") == []
    assert guard.scan("") == []


def test_blocked_error_names_the_rule_and_the_match():
    hits = guard.scan("nmap -p- 172.19.0.1")
    message = guard.blocked_error(hits)
    assert message.startswith("guard blocked this action (platform)")
    assert "172.19.0.1" in message


def test_never_block_carries_the_concrete_denylist():
    lines = guard.never_block(["10.9.9.9"])
    assert len(lines) >= 6
    assert lines[0].startswith("联网前必须先过一次判断")
    assert any("禁止为找答案上网" in line for line in lines)
    assert any("NET_REASON" in line for line in lines)
    assert any("10.9.9.9" in line for line in lines)
    assert any("172.19.0.1" in line for line in lines)




@pytest.mark.parametrize(
    "path",
    [
        "/opt/ipc-toolchain/overlay/arena.py",
        "lib/arena.py",
        "/opt/ipc-toolchain/overlay/bin/sapper-flag",
    ],
)
def test_overlay_accepts_paths_inside_the_overlay(path):
    normalized = overlay.normalize_path(path)
    assert normalized.startswith(overlay.OVERLAY_ROOT + "/")


@pytest.mark.parametrize(
    "path",
    [
        "/opt/sapper/lib/arena.py",
        "/opt/ipc-toolchain/overlay/../../etc/passwd",
        "/workspace/shared/exploit.py",
        "/opt/ipc-toolchain/overlay",
        "",
    ],
)
def test_overlay_rejects_everything_else(path):
    with pytest.raises(overlay.RepairRejected):
        overlay.normalize_path(path)


def test_overlay_plan_records_hashes_and_diff():
    meta = overlay.plan("old\n", "old\nnew\n")
    assert len(meta["sha256_before"]) == 64
    assert meta["sha256_after"] != meta["sha256_before"]
    assert "+new" in meta["diff"]
    assert meta["bytes_after"] == len(b"old\nnew\n")


def test_overlay_rejects_empty_or_oversized_content():
    with pytest.raises(overlay.RepairRejected):
        overlay.validate_content("   ")
    with pytest.raises(overlay.RepairRejected):
        overlay.validate_content("x" * (overlay.MAX_REPAIR_BYTES + 1))




def _board(now=None, **kwargs):
    now = now or datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    cards = kwargs.pop("cards", ())
    return Board(now=now, caps=Caps(remote_instances=3, seats=10, per_project=2), projects=tuple(cards), **kwargs)


def test_state_render_is_short_and_structured():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    cards = (
        ProjectCard(
            id="proj_001",
            title="A-03",
            status="running",
            seats=(Seat("amber", started_at=now - timedelta(minutes=34)),),
            created_at=now - timedelta(minutes=40),
            last_fact_at=now - timedelta(minutes=6),
            facts_since_assign=2,
        ),
        ProjectCard(
            id="proj_002",
            title="B-02",
            status="running",
            parked_at=now - timedelta(minutes=5),
            parked_reason="no-new-evidence-45m",
        ),
    )
    text = state_doc.render(_board(now=now, cards=cards), seq=42, intel=["b 家族 = pivot 链"], next_steps=["A-03 走源码角度"])
    lines = text.splitlines()
    assert len(lines) <= 150
    assert lines[0].startswith("# STATE @ 2026-10-21T12:00")
    assert "seq=42" in lines[0]
    order = [line.split()[0] for line in lines[1:] if line.strip()]
    assert order[:3] == ["CAPS", "SCORE", "ALLOC"]
    assert "PARKED" in order and "QUEUE" in order and "INTEL" in order and "NEXT" in order
    assert "no-new-evidence-45m" in text


def test_state_never_leaks_flags_or_tokens():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    text = state_doc.render(
        _board(now=now),
        intel=["found flag{real_flag} and sk-abcdefghijklmnop BENCHMARK_TOKEN"],
        next_steps=["password=hunter2"],
    )
    assert "flag{" not in text
    assert "sk-abcdefghij" not in text
    assert "hunter2" not in text
    assert "[redacted]" in text


def test_state_write_is_fingerprint_gated(tmp_path):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    state = type("S", (), {"projects_dir": str(tmp_path)})()
    board = _board(now=now, cards=(ProjectCard(id="proj_001", status="running"),))

    first = state_doc.maybe_write(state, "proj_001", board, seq=1)
    assert first is not None and first.is_file()
    assert "CAPS" in state_doc.read_latest(state, "proj_001")

    assert state_doc.maybe_write(state, "proj_001", board, seq=2) is None

    changed = _board(
        now=now,
        cards=(ProjectCard(id="proj_001", status="running", seats=(Seat("amber"),)),),
    )
    assert state_doc.maybe_write(state, "proj_001", changed, seq=3) is not None
    assert state_doc.maybe_write(state, "proj_001", changed, seq=4, force=True) is not None


def test_state_prunes_old_snapshots(tmp_path):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    state = type("S", (), {"projects_dir": str(tmp_path)})()
    board = _board(now=now)
    for seq in range(1, state_doc.MAX_FILES + 6):
        state_doc.maybe_write(state, "proj_001", board, seq=seq, force=True)
    snapshots = sorted((tmp_path / "proj_001" / "state").glob("state-*.md"))
    assert len(snapshots) == state_doc.MAX_FILES
