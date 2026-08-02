from __future__ import annotations

from pathlib import Path

from backend.blackboard import graph_store
from backend.core.wp_writer import validate_writeup


def verify_flag_and_wp(db, project_id: str, wp_dir: Path) -> dict:
    """Confirm flag + WP existence. Returns {ok, flag, wp_path, reasons}."""
    reasons: list[str] = []
    with db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            return {"ok": False, "reasons": ["project not found"]}
        flag = row["flag"]
        wp_path = row["wp_path"]
        has_goal_edge = conn.execute(
            "SELECT 1 FROM intents WHERE project_id = ? AND to_fact_id = 'goal'", (project_id,)
        ).fetchone() is not None

    if not flag:
        reasons.append("no flag recorded")
    if not has_goal_edge:
        reasons.append("no completion (goal) edge in graph")
    if not wp_path or not Path(wp_path).is_file():
        reasons.append("writeup file missing")
    elif errors := validate_writeup(
        Path(wp_path).read_text(encoding="utf-8"),
        expected_flag=flag,
        require_complete=True,
    ):
        reasons.extend(f"invalid writeup: {error}" for error in errors)

    return {"ok": not reasons, "flag": flag, "wp_path": wp_path, "reasons": reasons}
