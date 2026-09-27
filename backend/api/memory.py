from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.api.deps import get_state
from backend.core.state import AppState
from backend.blackboard import graph_store
from backend.core.redaction import redact_object
from backend.memory.exporter.obsidian import export_obsidian
from backend.memory.memory_search import search as mem_search
from backend.memory.memory_store import CATEGORIES, Memory

router = APIRouter(tags=["memory"])


class AddMemoryRequest(BaseModel):
    category: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)


@router.get("/memory", response_model=list[Memory])
def list_memory(category: str | None = None, state: AppState = Depends(get_state)):
    memories = state.memory.list(category)
    with state.db.connect() as conn:
        flags = {
            row["id"]: row["flag"]
            for row in conn.execute("SELECT id,flag FROM projects WHERE flag IS NOT NULL").fetchall()
        }
    return [
        item.__class__.model_validate(redact_object(item.model_dump(mode="json"), flags.get(item.project_id)))
        for item in memories
    ]


@router.post("/memory", response_model=Memory, status_code=201)
def add_memory(body: AddMemoryRequest, state: AppState = Depends(get_state)):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    mem = state.memory.add(body.category, body.title, body.content, body.tags, source="human")
    state.logger.memory("memory_added", title=body.title, category=body.category)
    return mem


@router.delete("/memory/{memory_id}", status_code=204)
def delete_memory(memory_id: str, state: AppState = Depends(get_state)):
    if not state.memory.delete(memory_id):
        raise HTTPException(404, "Memory not found")


@router.get("/memory/search")
def search_memory(q: str, category: str | None = None, limit: int = 5, state: AppState = Depends(get_state)):
    results = mem_search(state.memory, q, category=category, limit=limit)
    with state.db.connect() as conn:
        flags = {
            row["id"]: row["flag"]
            for row in conn.execute("SELECT id,flag FROM projects WHERE flag IS NOT NULL").fetchall()
        }
    return [
        {"memory": redact_object(m.model_dump(), flags.get(m.project_id)), "score": s}
        for m, s in results
    ]


@router.get("/memory/catalog")
def memory_catalog(state: AppState = Depends(get_state)):
    return {"children": state.catalog.tree()}


@router.get("/memory/catalog/{entry_id}")
def memory_catalog_entry(entry_id: str, state: AppState = Depends(get_state)):
    entry = state.catalog.get(entry_id)
    if entry is None:
        raise HTTPException(404, "Catalog document not found")
    return {
        **entry.to_dict(),
        "markdown": state.catalog.document(entry_id),
        "html": state.catalog.html_document(entry_id),
    }


@router.get("/memory/catalog/{entry_id}/document")
def memory_catalog_document(
    entry_id: str,
    state: AppState = Depends(get_state),
):
    entry = state.catalog.get(entry_id)
    if entry is None:
        raise HTTPException(404, "Catalog document not found")
    return Response(
        content=state.catalog.document(entry_id),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'inline; filename="{entry.id}.md"',
        },
    )


@router.post("/memory/derive")
def derive_memory(state: AppState = Depends(get_state)):
    vault = export_obsidian(state.memory, state.memory_export_dir / "vault")
    return {"vault": str(vault)}
