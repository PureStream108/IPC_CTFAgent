from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.archive import list_archived_projects, project_archive_id
from backend.core.state import AppState
from backend.filename_util import numbered_filename

router = APIRouter(tags=["wp"])


def _completed_writeups(state: AppState) -> list[dict]:
    with state.db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, wp_path, updated_at, created_at
            FROM projects
            WHERE status = 'completed' AND wp_path IS NOT NULL
            ORDER BY created_at, id
            """
        ).fetchall()
    used: set[str] = set()
    out: list[dict] = []
    live_archive_ids: set[str] = set()
    for row in rows:
        path = Path(row["wp_path"])
        if not path.exists():
            continue
        filename = numbered_filename(row["title"], ".md", used, fallback=row["id"])
        used.add(filename)
        archive_id = project_archive_id(str(row["id"]), str(row["created_at"]))
        live_archive_ids.add(archive_id)
        out.append(
            {
                "project_id": row["id"],
                "title": row["title"],
                "filename": filename,
                "content": path.read_text(encoding="utf-8"),
                "updated_at": row["updated_at"],
                "archive_id": archive_id,
                "archived": False,
            }
        )
    for archive in list_archived_projects(state):
        if archive["archive_id"] in live_archive_ids:
            continue
        path = state.wp_export_dir / archive["wp_filename"]
        out.append(
            {
                "project_id": f"archive:{archive['archive_id']}",
                "source_project_id": archive["project_id"],
                "title": archive["title"],
                "filename": archive["wp_filename"],
                "content": path.read_text(encoding="utf-8"),
                "updated_at": archive["archived_at"],
                "archive_id": archive["archive_id"],
                "archived": True,
            }
        )
    return out


@router.get("/projects/{project_id}/wp")
def get_wp(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    wp_path = row["wp_path"]
    if not wp_path:
        raise HTTPException(404, "No writeup yet")
    p = Path(wp_path)
    if not p.exists():
        raise HTTPException(404, "Writeup file missing")
    return Response(content=p.read_text(encoding="utf-8"), media_type="text/markdown")


@router.get("/wp/completed")
def completed_wp(state: AppState = Depends(get_state)):
    return {"writeups": _completed_writeups(state)}


@router.post("/wp/derive")
def derive_wp(state: AppState = Depends(get_state)):
    target = state.wp_export_dir
    target.mkdir(parents=True, exist_ok=True)
    writeups = _completed_writeups(state)
    files: list[str] = []
    with state.export_lock:
        used = {path.name for path in target.glob("*.md")}
        for item in writeups:
            while True:
                filename = numbered_filename(
                    item["title"], ".md", used, fallback=item["project_id"]
                )
                try:
                    with (target / filename).open("x", encoding="utf-8") as fh:
                        fh.write(item["content"])
                except FileExistsError:
                    # Another process may have exported after our directory
                    # scan. Reserve that name and retry with the next suffix.
                    used.add(filename)
                    continue
                used.add(filename)
                files.append(filename)
                break
    return {"dir": str(target), "files": files}


@router.get("/wp")
def list_wp(state: AppState = Depends(get_state)):
    files = sorted(state.wp_dir.glob("*.md"))
    return {"writeups": [f.name for f in files]}
