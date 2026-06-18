from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from backend.mcp.base import MCPServer

_BUNDLED_CHROME_PATHS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)
_BUNDLED_GHIDRA_HEADLESS_PATHS = (
    "/opt/ghidra/support/analyzeHeadless",
)
_BUNDLED_NM_PATHS = (
    "/usr/bin/nm",
    "/bin/nm",
)
_BUNDLED_OBJDUMP_PATHS = (
    "/usr/bin/objdump",
    "/bin/objdump",
)


class _TitleAndTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._hidden_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        elif self._hidden_depth == 0:
            self.text_parts.append(text)


def _html_summary(body: str, max_chars: int = 6000) -> tuple[str, str]:
    parser = _TitleAndTextParser()
    parser.feed(body)
    title = " ".join(parser.title_parts).strip()
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return title, text[:max_chars]


def _tool_unavailable(tool: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "tool": tool, "error": detail, **extra}


def _first_existing_path(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        if path and Path(path).exists():
            return path
    return None


def _configured_or_bundled(env_name: str, bundled_paths: tuple[str, ...]) -> str | None:
    configured = os.environ.get(env_name, "").strip()
    candidates = (configured, *bundled_paths) if configured else bundled_paths
    return _first_existing_path(candidates)


def _chrome_bin() -> str | None:
    return _configured_or_bundled("IPC_CHROME_BIN", _BUNDLED_CHROME_PATHS)


def build_browser_mcp() -> MCPServer:
    server = MCPServer(name="browser", description="Real browser/HTTP verification tools")

    @server.tool(
        name="navigate",
        description="Fetch a URL and return status, final URL, title, and visible text.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "number", "default": 12},
            },
            "required": ["url"],
        },
    )
    def navigate(url: str, timeout: float = 12):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": "IPC_CTFAgent/1.0"},
            )
        except requests.RequestException as exc:
            return _tool_unavailable("browser.navigate", str(exc), url=url)
        content_type = resp.headers.get("content-type", "")
        body = resp.text if "text" in content_type or "html" in content_type or not content_type else ""
        title, text = _html_summary(body)
        return {
            "available": True,
            "url": url,
            "final_url": resp.url,
            "status": resp.status_code,
            "title": title,
            "text": text,
            "content_type": content_type,
        }

    @server.tool(
        name="screenshot",
        description="Capture a screenshot using Playwright or a Chrome/Chromium CLI if installed.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "path": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 15000},
            },
            "required": ["url"],
        },
    )
    def screenshot(url: str, path: str | None = None, timeout_ms: int = 15000):
        out = Path(path) if path else Path(tempfile.gettempdir()) / f"ipc_browser_{int(time.time()*1000)}.png"
        chrome = _chrome_bin()
        if not chrome:
            return _tool_unavailable(
                "browser.screenshot",
                "Docker-bundled Chromium not found; rebuild ipc-app or set IPC_CHROME_BIN to an in-container path.",
                url=url,
                path=str(out),
            )
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--screenshot={out}",
            "--window-size=1365,900",
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(1, timeout_ms / 1000))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _tool_unavailable("browser.screenshot", str(exc), url=url, path=str(out))
        if proc.returncode != 0 or not out.exists():
            return _tool_unavailable(
                "browser.screenshot",
                (proc.stderr or proc.stdout or "Chrome did not create a screenshot").strip(),
                url=url,
                path=str(out),
            )
        return {"available": True, "url": url, "path": str(out)}

    return server


def _ghidra_headless() -> str | None:
    configured = os.environ.get("IPC_GHIDRA_HEADLESS") or os.environ.get("GHIDRA_ANALYZE_HEADLESS")
    candidates = (configured, *_BUNDLED_GHIDRA_HEADLESS_PATHS) if configured else _BUNDLED_GHIDRA_HEADLESS_PATHS
    return _first_existing_path(candidates)


def _run_command(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    return proc.returncode == 0, output


def build_ghidra_mcp() -> MCPServer:
    server = MCPServer(name="ghidra", description="Ghidra headless analysis adapter")

    @server.tool(
        name="decompile",
        description="Run Ghidra headless analysis for a binary and return the analysis log.",
        input_schema={
            "type": "object",
            "properties": {
                "binary": {"type": "string"},
                "function": {"type": "string", "default": "main"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["binary"],
        },
    )
    def decompile(binary: str, function: str = "main", timeout: int = 120):
        binary_path = Path(binary)
        if not binary_path.exists():
            return _tool_unavailable("ghidra.decompile", f"binary not found: {binary}", binary=binary, function=function)
        headless = _ghidra_headless()
        if not headless:
            return _tool_unavailable(
                "ghidra.decompile",
                "Docker-bundled Ghidra analyzeHeadless not found; rebuild ipc-app or set IPC_GHIDRA_HEADLESS to an in-container path.",
                binary=binary,
                function=function,
            )
        with tempfile.TemporaryDirectory(prefix="ipc_ghidra_") as tmp:
            cmd = [headless, tmp, "ipc_project", "-import", str(binary_path), "-deleteProject"]
            ok, output = _run_command(cmd, timeout=timeout)
        return {
            "available": ok,
            "binary": binary,
            "function": function,
            "pseudocode": "" if ok else None,
            "analysis_log": output[-12000:],
        }

    @server.tool(
        name="list_functions",
        description="List symbols/functions with nm/objdump as a lightweight binary-analysis adapter.",
        input_schema={"type": "object", "properties": {"binary": {"type": "string"}}, "required": ["binary"]},
    )
    def list_functions(binary: str):
        binary_path = Path(binary)
        if not binary_path.exists():
            return _tool_unavailable("ghidra.list_functions", f"binary not found: {binary}", binary=binary)
        cmd = None
        nm = _configured_or_bundled("IPC_NM_BIN", _BUNDLED_NM_PATHS)
        objdump = _configured_or_bundled("IPC_OBJDUMP_BIN", _BUNDLED_OBJDUMP_PATHS)
        if nm:
            cmd = [nm, "-C", str(binary_path)]
        elif objdump:
            cmd = [objdump, "-t", str(binary_path)]
        if not cmd:
            return _tool_unavailable("ghidra.list_functions", "Neither nm nor objdump is available.", binary=binary)
        ok, output = _run_command(cmd, timeout=30)
        names: list[str] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-2].upper() in {"T", "W", "FUNC", "F"}:
                names.append(parts[-1])
        return {"available": ok, "binary": binary, "functions": names[:500], "raw": output[-4000:] if not ok else ""}

    return server


def _zap_base() -> str:
    return os.environ.get("ZAP_API_URL", "http://ipc-zap:8080").rstrip("/")


def _zap_get(path: str, **params: Any) -> dict[str, Any]:
    api_key = os.environ.get("ZAP_API_KEY")
    if api_key:
        params["apikey"] = api_key
    resp = requests.get(f"{_zap_base()}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def build_zap_mcp() -> MCPServer:
    server = MCPServer(name="zap", description="OWASP ZAP API adapter")

    @server.tool(
        name="spider",
        description="Run ZAP spider against a target URL and return discovered URLs.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    def spider(url: str):
        try:
            scan = _zap_get("/JSON/spider/action/scan/", url=url)
            scan_id = scan.get("scan")
            urls = _zap_get("/JSON/core/view/urls/", baseurl=url).get("urls", [])
        except (requests.RequestException, ValueError) as exc:
            return _tool_unavailable("zap.spider", str(exc), url=url, urls_found=[])
        return {"available": True, "url": url, "scan": scan_id, "urls_found": urls}

    @server.tool(
        name="active_scan",
        description="Run a ZAP active scan against a target and return current alerts.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    def active_scan(url: str):
        try:
            scan = _zap_get("/JSON/ascan/action/scan/", url=url)
            alerts = _zap_get("/JSON/core/view/alerts/", baseurl=url).get("alerts", [])
        except (requests.RequestException, ValueError) as exc:
            return _tool_unavailable("zap.active_scan", str(exc), url=url, alerts=[])
        return {"available": True, "url": url, "scan": scan.get("scan"), "alerts": alerts}

    return server
