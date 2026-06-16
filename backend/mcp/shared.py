from __future__ import annotations

from backend.mcp.base import MCPServer


def build_browser_mcp() -> MCPServer:
    server = MCPServer(name="browser", description="Headless Chromium via Playwright (shared)")

    @server.tool(
        name="navigate",
        description="Open a URL in a real headless browser and return status + title + text.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    def navigate(url: str):
        return {"url": url, "status": 200, "title": "(stub)", "text": "", "note": "browser MCP stub"}

    @server.tool(
        name="screenshot",
        description="Capture a screenshot of the current page; returns a file path.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    def screenshot(url: str):
        return {"url": url, "path": "/tmp/screenshot.png", "note": "browser MCP stub"}

    return server


def build_ghidra_mcp() -> MCPServer:
    server = MCPServer(name="ghidra", description="Ghidra headless analysis (shared)")

    @server.tool(
        name="decompile",
        description="Decompile a function in a binary and return pseudo-C.",
        input_schema={
            "type": "object",
            "properties": {
                "binary": {"type": "string"},
                "function": {"type": "string", "default": "main"},
            },
            "required": ["binary"],
        },
    )
    def decompile(binary: str, function: str = "main"):
        return {"binary": binary, "function": function, "pseudocode": "// ghidra MCP stub", "note": "stub"}

    @server.tool(
        name="list_functions",
        description="List functions discovered in a binary.",
        input_schema={"type": "object", "properties": {"binary": {"type": "string"}}, "required": ["binary"]},
    )
    def list_functions(binary: str):
        return {"binary": binary, "functions": ["main"], "note": "ghidra MCP stub"}

    return server


def build_zap_mcp() -> MCPServer:
    server = MCPServer(name="zap", description="OWASP ZAP active/passive scanning (shared)")

    @server.tool(
        name="spider",
        description="Spider a target URL to enumerate endpoints.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    def spider(url: str):
        return {"url": url, "urls_found": [url], "note": "zap MCP stub"}

    @server.tool(
        name="active_scan",
        description="Run an active vulnerability scan against a target and return alerts.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    def active_scan(url: str):
        return {"url": url, "alerts": [], "note": "zap MCP stub"}

    return server
