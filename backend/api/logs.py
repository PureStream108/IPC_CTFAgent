from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.archive import list_archived_projects, project_archive_id
from backend.core.state import AppState
from backend.filename_util import numbered_filename

router = APIRouter(tags=["logs"])
LOG_GROUPS = (
    ("project", "project_log"),
    ("llm", "llm_log"),
    ("tool", "tool_log"),
    ("memory", "memory_log"),
)


class LogToggle(BaseModel):
    enabled: bool


@router.get("/logs/status")
def log_status(state: AppState = Depends(get_state)):
    return {"enabled": state.logger.enabled}


@router.put("/logs/status")
def set_log_status(body: LogToggle, state: AppState = Depends(get_state)):
    state.logger.set_enabled(body.enabled)
    state.config.log_enabled = body.enabled
    state.save_config()
    return {"enabled": state.logger.enabled}


@router.get("/logs/projects")
def read_project_logs(limit: int = 500, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    logs = []
    live_archive_ids: set[str] = set()
    for project in projects:
        live_archive_ids.add(project_archive_id(project.id, project.created_at))
        item = {
            "project_id": project.id,
            "title": project.title,
            "status": project.status,
        }
        for kind, key in LOG_GROUPS:
            item[key] = {
                "filename": project.log_filename or f"{project.id}.jsonl",
                "entries": state.logger.read_log(kind, project.id, limit),
            }
        logs.append(item)
    for archive in list_archived_projects(state):
        if archive["archive_id"] in live_archive_ids:
            continue
        item = {
            "project_id": f"archive:{archive['archive_id']}",
            "source_project_id": archive["project_id"],
            "title": archive["title"],
            "status": "solved",
            "archived": True,
        }
        for kind, key in LOG_GROUPS:
            path = (
                state.log_export_dir
                / state.logger.KINDS[kind]
                / archive["log_filename"]
            )
            item[key] = {
                "filename": archive["log_filename"],
                "entries": state.logger.read_file(path, limit),
            }
        logs.append(item)
    return {
        "logs": logs
    }


@router.post("/logs/derive")
def derive_project_logs(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    target = state.log_export_dir
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[str]] = {
        state.logger.KINDS[kind]: [] for kind, _ in LOG_GROUPS
    }
    folders = {
        kind: target / state.logger.KINDS[kind]
        for kind, _ in LOG_GROUPS
    }
    for kind, _ in LOG_GROUPS:
        folders[kind].mkdir(parents=True, exist_ok=True)

    with state.export_lock:
        # One task uses one suffix across all four log groups.
        used = {
            path.name
            for folder in folders.values()
            for path in folder.glob("*.log")
        }
        for project in projects:
            contents = {
                kind: state.logger.jsonl_text(
                    state.logger.read_log(kind, project.id, None)
                )
                for kind, _ in LOG_GROUPS
            }
            while True:
                filename = numbered_filename(
                    project.title, ".log", used, fallback=project.id
                )
                created = []
                try:
                    for kind, _ in LOG_GROUPS:
                        path = folders[kind] / filename
                        with path.open("x", encoding="utf-8") as fh:
                            fh.write(contents[kind])
                        created.append(path)
                except FileExistsError:
                    for path in created:
                        path.unlink(missing_ok=True)
                    used.add(filename)
                    continue
                except Exception:
                    for path in created:
                        path.unlink(missing_ok=True)
                    raise

                used.add(filename)
                for kind, _ in LOG_GROUPS:
                    files[state.logger.KINDS[kind]].append(filename)
                break
    return {"dir": str(target), "files": files}


@router.get("/logs/{project_id}")
def read_logs(project_id: str, kind: str = "project", limit: int = 500, state: AppState = Depends(get_state)):
    return {"entries": state.logger.read_log(kind, project_id, limit)}
