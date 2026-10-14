"""Enforce the repository's coverage floors by architectural module group."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GROUPS = {
    "agent-core": ("backend/agent/", "backend/members/base_member.py", "backend/core/orchestrator.py"),
    "competition": ("backend/competition/",),
}
FLOOR = 80.0
BASELINE_PATH = Path("coverage-baseline.json")


def main() -> int:
    path = Path("coverage.json")
    if not path.exists():
        print("coverage.json is missing; run pytest --cov --cov-report=json", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else {}
    failed = False
    for name, prefixes in GROUPS.items():
        selected = {
            file: report for file, report in files.items()
            if any(file.replace("\\", "/").startswith(prefix) or file.replace("\\", "/").endswith(prefix) for prefix in prefixes)
        }
        covered = sum(int(report.get("summary", {}).get("covered_lines", 0)) for report in selected.values())
        total = sum(int(report.get("summary", {}).get("num_statements", 0)) for report in selected.values())
        percent = 100.0 if total == 0 else 100.0 * covered / total
        print(f"{name}: {percent:.2f}% ({covered}/{total})")
        baseline_percent = float((baseline.get(name) or {}).get("line_percent", 0))
        # The saved baseline is rounded to two decimals. Compare at the same
        # precision so a stored 80.81% does not reject its exact 80.806% run.
        if baseline_percent and round(percent, 2) < baseline_percent:
            print(f"{name}: below saved baseline {baseline_percent:.2f}%", file=sys.stderr)
            failed = True
        if total == 0 or percent < FLOOR:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
