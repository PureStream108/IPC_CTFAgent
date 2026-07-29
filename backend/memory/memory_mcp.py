from __future__ import annotations

import asyncio
from typing import Any, Callable, Literal

from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.memory.memory_search import search
from backend.memory.memory_store import MemoryStore
from backend.tools.catalog import ToolCatalog


def build_memory_mcp(
    store: MemoryStore,
    *,
    catalog: ToolCatalog | None = None,
    lifespan: Callable | None = None,
) -> MCPServer:
    catalog = catalog or ToolCatalog.load()
    server = create_mcp_server(
        "memory",
        "Search and read CTF experience memory",
        lifespan=lifespan,
    )

    @server.tool(
        name="memory_search",
        description=(
            "Search past CTF experience by associated keywords. Returns the most "
            "relevant memory summaries. Use the returned id with memory_get for full content."
        ),
    )
    async def memory_search(
        query: str,
        category: Literal["knowledge", "tool_usage", "exploit", "lessons"] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        results = await asyncio.to_thread(
            search, store, query, category=category, limit=limit
        )
        memory_hits = [
            {
                "id": m.id,
                "category": m.category,
                "title": m.title,
                "tags": m.tags,
                "score": round(s, 2),
                "preview": m.content[:160],
                "entry_type": "memory",
                "doc_id": None,
                "doc_url": None,
            }
            for m, s in results
        ]
        catalog_hits = []
        if category in (None, "tool_usage"):
            catalog_hits = [
                {
                    "id": entry.id,
                    "category": "tool_usage",
                    "title": entry.title,
                    "tags": entry.tags,
                    "score": round(score, 2),
                    "preview": entry.summary,
                    "entry_type": "catalog",
                    "doc_id": entry.id,
                    "doc_url": f"/memory/catalog/{entry.id}/document",
                }
                for entry, score in await asyncio.to_thread(
                    catalog.search, query, limit
                )
            ]
        # Preserve the established contract: user/project experience is more
        # important than generic built-in documentation. Catalog hits fill the
        # remaining slots.
        return [*memory_hits, *catalog_hits][:limit]

    @server.tool(
        name="memory_get",
        description="Fetch the full content of a memory by id.",
    )
    async def memory_get(id: str) -> dict[str, Any]:
        mem = await asyncio.to_thread(store.get, id)
        if mem is None:
            return {"error": f"no memory with id {id}"}
        return mem.model_dump()

    @server.tool(
        name="memory_catalog",
        description="Browse the built-in tool/MCP/language/library documentation tree.",
    )
    async def memory_catalog(path: str | None = None) -> dict[str, Any]:
        return catalog.browse(path)

    @server.tool(
        name="memory_doc",
        description="Read one complete built-in catalog document as Markdown.",
    )
    async def memory_doc(id: str) -> dict[str, Any]:
        entry = catalog.get(id)
        if entry is None:
            return {"error": f"no catalog document with id {id}"}
        return {
            **entry.to_dict(),
            "markdown": catalog.document(id),
        }

    return server
