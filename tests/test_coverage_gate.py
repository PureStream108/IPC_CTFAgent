"""Regression checks for the coverage gate's saved-baseline comparison."""
import json

from scripts import check_coverage


def reports(tmp_path, monkeypatch, covered, statements):
    monkeypatch.chdir(tmp_path)
    files = {
        "backend/agent/runtime.py": {"summary": {"covered_lines": 4, "num_statements": 5}},
        "backend/competition/service.py": {"summary": {"covered_lines": covered, "num_statements": statements}},
    }
    (tmp_path / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    (tmp_path / "coverage-baseline.json").write_text(
        json.dumps({"agent-core": {"line_percent": 80}, "competition": {"line_percent": 80.81}}), encoding="utf-8",
    )


def test_gate_accepts_the_exact_run_used_to_save_a_rounded_baseline(tmp_path, monkeypatch):
    reports(tmp_path, monkeypatch, 2505, 3100)
    assert check_coverage.main() == 0


def test_rounding_does_not_bypass_the_eighty_percent_floor(tmp_path, monkeypatch):
    reports(tmp_path, monkeypatch, 79999, 100000)
    # Remove the stricter saved baseline to exercise the floor itself.
    (tmp_path / "coverage-baseline.json").unlink()
    assert check_coverage.main() == 1


def test_gate_rejects_a_missing_module_group(tmp_path, monkeypatch):
    reports(tmp_path, monkeypatch, 0, 0)
    assert check_coverage.main() == 1
