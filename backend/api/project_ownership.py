from __future__ import annotations

from typing import Any


def standalone(project_id: str = "") -> dict:
    return {
        "ownership": "standalone",
        "project_id": project_id,
        "workflow_id": "",
        "run_id": "",
        "challenge_id": "",
        "run_status": "",
        "challenge_state": "",
    }


def project_ownership(conn: Any, project_ids: list[str] | None = None) -> dict[str, dict]:
    """Project how each Project is owned without adding a second mode column.

    competition  -> the Project is one challenge of a CompetitionRun
    legacy_import -> the Project came from a pre-run workflow import
    standalone   -> local draft or read-only history
    """

    owned: dict[str, dict] = {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (c.project_id)
               c.project_id, c.id AS challenge_id, c.state AS challenge_state,
               r.id AS run_id, r.status AS run_status, r.workflow_id
        FROM competition_challenges c
        JOIN competition_runs r ON r.id = c.run_id
        WHERE c.project_id IS NOT NULL
        ORDER BY c.project_id, r.created_at DESC, c.id DESC
        """
    ).fetchall()
    for row in rows:
        pid = str(row["project_id"])
        owned[pid] = {
            "ownership": "competition",
            "project_id": pid,
            "workflow_id": str(row["workflow_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "challenge_id": str(row["challenge_id"] or ""),
            "run_status": str(row["run_status"] or ""),
            "challenge_state": str(row["challenge_state"] or ""),
        }
    legacy = conn.execute(
        """
        SELECT DISTINCT ON (project_id) project_id, workflow_id
        FROM workflow_challenges
        WHERE project_id IS NOT NULL
        ORDER BY project_id, workflow_id
        """
    ).fetchall()
    for row in legacy:
        pid = str(row["project_id"])
        if pid in owned:
            continue
        owned[pid] = {
            "ownership": "legacy_import",
            "project_id": pid,
            "workflow_id": str(row["workflow_id"] or ""),
            "run_id": "",
            "challenge_id": "",
            "run_status": "",
            "challenge_state": "",
        }
    if project_ids is None:
        return owned
    wanted = {str(item) for item in project_ids}
    return {pid: value for pid, value in owned.items() if pid in wanted}


def project_ownership_for(state, project_id: str) -> dict:
    with state.db.connect() as conn:
        return project_ownership(conn, [project_id]).get(project_id) or standalone(project_id)
