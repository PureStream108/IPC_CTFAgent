from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import get_state
from backend.core.state import AppState
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
    return state.memory.list(category)


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
    return [{"memory": m.model_dump(), "score": s} for m, s in results]


@router.post("/memory/derive")
def derive_memory(state: AppState = Depends(get_state)):
    vault = export_obsidian(state.memory, state.root / "memory" / "export" / "vault")
    return {"vault": str(vault)}
