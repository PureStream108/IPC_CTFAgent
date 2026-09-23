from backend.mcp.mcp_server import create_mcp_server


def build_skills_mcp(store):
    server = create_mcp_server("skills", "Discover and load versioned CTF skills, including cross-category resources.")

    @server.tool()
    def discover(category: str = "misc") -> dict:
        return {"preferred": store.for_category(category), "packages": store.snapshot()}

    @server.tool()
    def read(repository: str, revision: str, path: str) -> dict:
        return {"content": store.read(repository, revision, path), "revision": revision}

    return server
