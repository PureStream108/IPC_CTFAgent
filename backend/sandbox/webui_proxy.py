from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit, urlunsplit

import requests

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _strip_hop_by_hop(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


@dataclass(slots=True)
class ProxyHandle:
    project_id: str
    member: str
    target_host: str
    target_port: int
    local_host: str
    local_port: int
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def url(self) -> str:
        return f"http://{self.local_host}:{self.local_port}"


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IPCWebUIProxy/1.0"

    _STREAMING_CONTENT_TYPES: ClassVar[tuple[str, ...]] = (
        "text/event-stream",
        "application/octet-stream",
    )

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def log_message(self, fmt: str, *args) -> None:
        return

    def _proxy(self) -> None:
        handle: ProxyHandle = self.server.proxy_handle  # type: ignore[attr-defined]
        body = self._read_body()
        upstream_url = urlunsplit(
            (
                "http",
                f"{handle.target_host}:{handle.target_port}",
                urlsplit(self.path).path,
                urlsplit(self.path).query,
                "",
            )
        )
        headers = _strip_hop_by_hop(dict(self.headers.items()))
        headers["Host"] = f"{handle.target_host}:{handle.target_port}"
        try:
            upstream = requests.request(
                self.command,
                upstream_url,
                headers=headers,
                data=body,
                allow_redirects=False,
                stream=True,
                timeout=90,
            )
        except requests.RequestException as exc:
            payload = f"proxy error: {exc}".encode("utf-8", errors="replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        content_type = upstream.headers.get("content-type", "").lower()
        should_stream = any(kind in content_type for kind in self._STREAMING_CONTENT_TYPES)
        if should_stream:
            self._stream_response(upstream)
        else:
            self._buffered_response(upstream)

    def _read_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if not length:
            return None
        try:
            size = int(length)
        except ValueError:
            return None
        if size <= 0:
            return None
        return self.rfile.read(size)

    def _stream_response(self, upstream: requests.Response) -> None:
        self.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() in _HOP_BY_HOP_HEADERS or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            upstream.close()

    def _buffered_response(self, upstream: requests.Response) -> None:
        content = upstream.content
        self.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() in _HOP_BY_HOP_HEADERS or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)
        upstream.close()


class WebUIProxyManager:
    def __init__(self, bind_host: str = "127.0.0.1") -> None:
        self.bind_host = bind_host
        self._handles: dict[tuple[str, str, int], ProxyHandle] = {}
        self._lock = threading.Lock()

    def register(self, project_id: str, member: str, target_host: str, target_port: int) -> ProxyHandle:
        key = (project_id, member, target_port)
        with self._lock:
            handle = self._handles.get(key)
            if handle is not None:
                return handle

            httpd = ThreadingHTTPServer((self.bind_host, 0), _ProxyHandler)
            httpd.daemon_threads = True
            local_port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True, name=f"ipc-webui-{project_id}-{member}")
            handle = ProxyHandle(
                project_id=project_id,
                member=member,
                target_host=target_host,
                target_port=target_port,
                local_host=self.bind_host,
                local_port=local_port,
                server=httpd,
                thread=thread,
            )
            httpd.proxy_handle = handle  # type: ignore[attr-defined]
            thread.start()
            self._handles[key] = handle
            return handle

    def close_member(self, project_id: str, member: str) -> None:
        with self._lock:
            keys = [key for key in self._handles if key[0] == project_id and key[1] == member]
            handles = [self._handles.pop(key) for key in keys]
        for handle in handles:
            self._shutdown(handle)

    def close_project(self, project_id: str) -> None:
        with self._lock:
            keys = [key for key in self._handles if key[0] == project_id]
            handles = [self._handles.pop(key) for key in keys]
        for handle in handles:
            self._shutdown(handle)

    def close_all(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            self._shutdown(handle)

    def _shutdown(self, handle: ProxyHandle) -> None:
        handle.server.shutdown()
        handle.server.server_close()
        handle.thread.join(timeout=1)


webui_proxy_manager = WebUIProxyManager()
