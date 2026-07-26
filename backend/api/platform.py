from __future__ import annotations

import shutil

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState
from backend.platform.adapter import HttpJsonAdapter
from backend.platform.mapping import FieldMapping, PlatformChallenge

router = APIRouter(prefix="/api/platform", tags=["platform"])


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: FieldMapping


class ImportRequest(ChallengeRequest):
    select: list[str] | None = None


def _fetch(mapping: FieldMapping) -> tuple[HttpJsonAdapter, list[PlatformChallenge]]:
    adapter = HttpJsonAdapter(mapping)
    try:
        return adapter, adapter.fetch_challenges()
    except requests.RequestException as exc:
        raise HTTPException(502, f"platform request failed: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/challenges")
def preview_challenges(body: ChallengeRequest):
    _, challenges = _fetch(body.mapping)
    return {"challenges": [challenge.model_dump() for challenge in challenges]}


@router.post("/import", status_code=201)
def import_challenges(body: ImportRequest, state: AppState = Depends(get_state)):
    adapter, challenges = _fetch(body.mapping)
    by_id = {challenge.external_id: challenge for challenge in challenges}
    if body.select is None:
        selected = challenges
    else:
        missing = [external_id for external_id in body.select if external_id not in by_id]
        if missing:
            raise HTTPException(400, f"unknown external_id values: {missing}")
        selected = [by_id[external_id] for external_id in body.select]

    imported: list[dict[str, str]] = []
    created_project_ids: list[str] = []
    try:
        with state.db.connect() as conn:
            for challenge in selected:
                project_id = graph_store.create_project(
                    conn,
                    challenge.title,
                    body.mapping.list_url,
                    "capture the flag",
                    challenge.category,
                    external_id=challenge.external_id,
                )
                created_project_ids.append(project_id)
                for path in adapter.download_attachments(
                    challenge,
                    state.attachments_dir(project_id),
                ):
                    graph_store.create_attachment(
                        conn,
                        project_id,
                        path.name,
                        str(path),
                    )
                imported.append(
                    {
                        "external_id": challenge.external_id,
                        "project_id": project_id,
                        "title": challenge.title,
                        "category": challenge.category,
                    }
                )
    except requests.RequestException as exc:
        for project_id in created_project_ids:
            shutil.rmtree(state.projects_dir / project_id, ignore_errors=True)
        raise HTTPException(502, f"attachment download failed: {exc}") from exc
    except (OSError, TypeError, ValueError) as exc:
        for project_id in created_project_ids:
            shutil.rmtree(state.projects_dir / project_id, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc

    for item in imported:
        state.logger.project(
            "platform_project_imported",
            item["project_id"],
            external_id=item["external_id"],
            title=item["title"],
            category=item["category"],
        )
    return {"imported": imported}
