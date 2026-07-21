from __future__ import annotations

import asyncio
from typing import Any, Literal

from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.memory.memory_search import search
from backend.memory.memory_store import MemoryStore


def build_memory_mcp(store: MemoryStore) -> MCPServer:
    server = create_mcp_server("memory", "Search and read CTF experience memory")

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
        results = await asyncio.to_thread(search, store, query, category=category, limit=limit)
        return [
            {
                "id": m.id,
                "category": m.category,
                "title": m.title,
                "tags": m.tags,
                "score": round(s, 2),
                "preview": m.content[:160],
            }
            for m, s in results
        ]

    @server.tool(
        name="memory_get",
        description="Fetch the full content of a memory by id.",
    )
    async def memory_get(id: str) -> dict[str, Any]:
        mem = await asyncio.to_thread(store.get, id)
        if mem is None:
            return {"error": f"no memory with id {id}"}
        return mem.model_dump()

    return server
