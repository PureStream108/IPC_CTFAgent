from __future__ import annotations


from backend.mcp.base import MCPServer
from backend.memory.memory_search import search
from backend.memory.memory_store import CATEGORIES, MemoryStore


def build_memory_mcp(store: MemoryStore) -> MCPServer:
    server = MCPServer(name="memory", description="Search and read CTF experience memory")

    @server.tool(
        name="memory_search",
        description=(
            "Search past CTF experience by associated keywords. Returns the most "
            "relevant memory summaries. Use the returned id with memory_get for full content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "keywords, e.g. 'flask ssti pin'"},
                "category": {
                    "type": "string",
                    "enum": list(CATEGORIES),
                    "description": "optional filter: knowledge|tool_usage|exploit|lessons",
                },
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    )
    def memory_search(query: str, category: str | None = None, limit: int = 5):
        results = search(store, query, category=category, limit=limit)
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
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )
    def memory_get(id: str):
        mem = store.get(id)
        if mem is None:
            return {"error": f"no memory with id {id}"}
        return mem.model_dump()

    return server
