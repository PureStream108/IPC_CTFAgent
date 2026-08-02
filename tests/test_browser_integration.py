from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

pytest.importorskip("playwright.async_api")

from backend.mcp.mcp_client import MCPClient
from backend.mcp.shared import _BrowserSession, build_browser_mcp


_PAGE = b"""<!doctype html>
<html>
<head><title>Browser integration</title></head>
<body>
  <input id="upload" type="file">
  <a id="download" href="/download" download>download</a>
  <script>
    setTimeout(() => {
      const ready = document.createElement('div');
      ready.id = 'ready';
      ready.textContent = 'ready';
      document.body.appendChild(ready);
      console.error('token=console-secret');
      fetch('/api?token=request-secret');
      setTimeout(() => { throw new Error('secret=page-secret'); }, 0);
    }, 50);
  </script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api"):
            self._send(
                b'{"token":"response-secret","ok":true}',
                content_type="application/json",
            )
            return
        if self.path == "/download":
            self._send(
                b"download artifact",
                content_type="application/octet-stream",
                headers={"Content-Disposition": 'attachment; filename="evidence.txt"'},
            )
            return
        self._send(_PAGE, content_type="text/html; charset=utf-8")

    def _send(
        self,
        body: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        pass


@pytest.fixture
def browser_test_origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_browser_phase_one_flow(tmp_path: Path, browser_test_origin: str):
    member = tmp_path / "member"
    shared = tmp_path / "shared"
    member.mkdir()
    shared.mkdir()
    upload = member / "payload.txt"
    upload.write_text("upload payload", encoding="utf-8")
    session = _BrowserSession(
        project_id="proj_integration",
        member="aventurine",
        workdir=member,
        shared_dir=shared,
        artifact_root=member / "browser-artifacts",
        allowed_origins=[browser_test_origin],
    )
    server = build_browser_mcp(session)

    async def run() -> None:
        try:
            await session.page()
        except Exception as exc:
            detail = str(exc).lower()
            if "executable doesn't exist" in detail or "playwright install" in detail:
                pytest.skip("Playwright Chromium is not installed")
            raise

        async with MCPClient.in_process(server) as client:
            started = await client.call_tool(
                "network_log_start",
                {"capture_response_preview": True},
            )
            assert started["available"] is True
            navigated = await client.call_tool("navigate", {"url": browser_test_origin})
            assert navigated["available"] is True
            waited = await client.call_tool("wait_for", {"selector": "#ready"})
            assert waited["available"] is True
            settled = await client.call_tool(
                "wait_for", {"load_state": "networkidle", "timeout_ms": 5000}
            )
            assert settled["available"] is True

            network = await client.call_tool("network_log_list", {})
            api_event = next(event for event in network["events"] if "/api" in event["url"])
            assert "request-secret" not in api_event["url"]
            assert "response-secret" not in api_event["response_preview"]
            console = await client.call_tool("console_logs", {"levels": ["error"]})
            assert console["events"]
            assert "console-secret" not in console["events"][0]["text"]
            errors = await client.call_tool("page_errors", {})
            assert errors["events"]
            assert "page-secret" not in errors["events"][0]["message"]

            uploaded = await client.call_tool(
                "upload_file", {"selector": "#upload", "paths": [str(upload)]}
            )
            assert uploaded["files"] == [{"name": "payload.txt", "size": 14}]
            downloaded = await client.call_tool("download", {"selector": "#download"})
            assert downloaded["artifact_id"].startswith("download:")
            screenshot = await client.call_tool("screenshot", {})
            assert screenshot["artifact_id"].startswith("screenshot:")

    asyncio.run(run())
    metadata = member / "browser-artifacts" / "metadata.jsonl"
    assert len(metadata.read_text(encoding="utf-8").splitlines()) == 2
