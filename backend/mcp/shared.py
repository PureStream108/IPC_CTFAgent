from __future__ import annotations

import asyncio
import atexit
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from backend.mcp.mcp_server import MCPServer, create_mcp_server


_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[-_])(?:authorization|cookie|password|passwd|proxy-authorization|set-cookie|"
    r"token|secret|api[-_]?key|key)(?:$|[-_])",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)([\"']?\b(?:authorization|proxy-authorization|set-cookie|cookie|password|passwd|"
    r"token|secret|api[-_]?key)\b[\"']?)(\s*[:=]\s*)([\"']?)([^\s,;}]+)"
)
_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|set-cookie|cookie)\b(\s*[:=]\s*)[^\r\n]+"
)
_DEFAULT_RESOURCE_TYPES = {"document", "xhr", "fetch", "script"}
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/graphql-response+json",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    return bool(_SENSITIVE_KEY_RE.search(normalized))


def _redact_url(url: str) -> str:
    """Remove credentials and sensitive query values while preserving request identity."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return _redact_sensitive_assignments(url)[:4000]
    if not parts.scheme or not parts.netloc:
        return _redact_sensitive_assignments(url)[:4000]
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    userinfo = "[REDACTED]@" if parts.username is not None else ""
    query = urlencode(
        [
            (key, "[REDACTED]" if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, f"{userinfo}{host}{port}", parts.path, query, ""))[:4000]


def _redact_sensitive_assignments(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        quote = match.group(3)
        closing = quote if quote and match.group(4).endswith(quote) else ""
        return f"{match.group(1)}{match.group(2)}{quote}[REDACTED]{closing}"

    text = _SENSITIVE_HEADER_RE.sub(r"\1\2[REDACTED]", text)
    return _SENSITIVE_TEXT_RE.sub(replace, text)


def _redact_text(value: Any, max_chars: int = 4000) -> str:
    text = str(value or "")
    text = _redact_sensitive_assignments(text)
    text = re.sub(
        r"https?://[^\s\"'<>]+",
        lambda match: _redact_url(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    return text[:max_chars]


def _redact_data(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_data(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_data(item) for item in value]
    if isinstance(value, str):
        return _redact_url(value) if key.lower().endswith("url") else _redact_text(value)
    return value


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("only HTTP(S) URLs are allowed")
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    default_port = 80 if parts.scheme.lower() == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme.lower()}://{host}{suffix}"


class _EventBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: deque[dict[str, Any]] = deque(maxlen=limit)
        self.dropped_count = 0

    def append(self, event: dict[str, Any]) -> None:
        if len(self.items) == self.limit:
            self.dropped_count += 1
        self.items.append(event)

    def clear(self) -> None:
        self.items.clear()
        self.dropped_count = 0

    def list(self, *, after_id: int | None = None) -> list[dict[str, Any]]:
        floor = after_id or 0
        return [dict(event) for event in self.items if event["event_id"] > floor]


class _BrowserSession:
    """One isolated Playwright browser session for one Member solve."""

    def __init__(
        self,
        *,
        project_id: str | None = None,
        member: str | None = None,
        workdir: str | Path | None = None,
        shared_dir: str | Path | None = None,
        artifact_root: str | Path | None = None,
        event_limit: int | None = None,
        console_limit: int | None = None,
        error_limit: int | None = None,
        response_preview_bytes: int | None = None,
        artifact_max_bytes: int | None = None,
        allowed_origins: list[str] | None = None,
    ) -> None:
        env_workdir = os.environ.get("IPC_BROWSER_WORKDIR") or os.getcwd()
        self.project_id = project_id or os.environ.get("IPC_BROWSER_PROJECT_ID", "unknown")
        self.member = member or os.environ.get("IPC_BROWSER_MEMBER", Path(env_workdir).name)
        self.workdir = Path(workdir or env_workdir).resolve()
        self.shared_dir = Path(
            shared_dir or os.environ.get("IPC_BROWSER_SHARED_DIR", str(self.workdir.parent / "shared"))
        ).resolve()
        self.artifact_root = Path(
            artifact_root
            or os.environ.get("IPC_BROWSER_ARTIFACT_ROOT", str(self.workdir / "browser-artifacts"))
        ).resolve()
        self.event_limit = event_limit if event_limit is not None else _env_int(
            "IPC_BROWSER_EVENT_LIMIT", 200, minimum=1, maximum=1000
        )
        self.console_limit = console_limit if console_limit is not None else _env_int(
            "IPC_BROWSER_CONSOLE_LIMIT", 100, minimum=1, maximum=1000
        )
        self.error_limit = error_limit if error_limit is not None else _env_int(
            "IPC_BROWSER_ERROR_LIMIT", 50, minimum=1, maximum=1000
        )
        self.response_preview_bytes = response_preview_bytes if response_preview_bytes is not None else _env_int(
            "IPC_BROWSER_RESPONSE_PREVIEW_BYTES", 4096, minimum=1, maximum=16384
        )
        self.artifact_max_bytes = artifact_max_bytes if artifact_max_bytes is not None else _env_int(
            "IPC_BROWSER_ARTIFACT_MAX_BYTES", 50 * 1024 * 1024,
            minimum=1, maximum=50 * 1024 * 1024,
        )
        if allowed_origins is None:
            try:
                raw_origins = json.loads(os.environ.get("IPC_BROWSER_ALLOWED_ORIGINS", "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_origins = []
            allowed_origins = raw_origins if isinstance(raw_origins, list) else []
        self.allowed_origins = {_origin(str(value)) for value in allowed_origins}

        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self._pages: dict[str, Any] = {}
        self._page_ids: dict[int, str] = {}
        self._active_page_id: str | None = None
        self._next_page_number = 1
        self._next_event_id = 1
        self._network_events = _EventBuffer(self.event_limit)
        self._console_events = _EventBuffer(self.console_limit)
        self._page_errors = _EventBuffer(self.error_limit)
        self._network_active = False
        self._network_include_resources = False
        self._network_capture_preview = False
        self._network_preview_limit = self.response_preview_bytes
        self._network_page_id: str | None = None
        self._request_started: dict[int, float] = {}
        self._pending_tasks: set[asyncio.Task] = set()

    async def _ensure_started(self) -> None:
        if self._context is not None:
            return
        async with self._lock:
            if self._context is not None:
                return
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            self._context = await self._browser.new_context(accept_downloads=True)
            if self.allowed_origins:
                await self._context.route("**/*", self._route_request)
            self._context.on("request", self._on_request)
            self._context.on("response", self._on_response)
            self._context.on("requestfailed", self._on_request_failed)
            page = await self._context.new_page()
            self._register_page(page, preferred_id="main")
            self._context.on("page", self._on_new_page)

    async def _route_request(self, route: Any, request: Any) -> None:
        try:
            self.validate_url(str(request.url))
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    def _register_page(self, page: Any, *, preferred_id: str | None = None) -> str:
        existing = self._page_ids.get(id(page))
        if existing:
            return existing
        if preferred_id and preferred_id not in self._pages:
            page_id = preferred_id
        else:
            while True:
                page_id = f"page_{self._next_page_number:02d}"
                self._next_page_number += 1
                if page_id not in self._pages:
                    break
        self._pages[page_id] = page
        self._page_ids[id(page)] = page_id
        self._active_page_id = page_id
        try:
            page.on("console", lambda message, pid=page_id: self._record_console(pid, message))
            page.on("pageerror", lambda error, pid=page_id: self._record_page_error(pid, error))
            page.on("close", lambda _page=None, pid=page_id: self._page_closed(pid))
        except (AttributeError, TypeError):
            pass
        return page_id

    def _on_new_page(self, page: Any) -> None:
        self._register_page(page)

    def _page_closed(self, page_id: str) -> None:
        page = self._pages.pop(page_id, None)
        if page is not None:
            self._page_ids.pop(id(page), None)
        if self._active_page_id == page_id:
            self._active_page_id = next(reversed(self._pages), None) if self._pages else None

    async def page(self, page_id: str | None = None):
        await self._ensure_started()
        selected = page_id or self._active_page_id
        if selected is None or selected not in self._pages:
            raise ValueError(f"unknown page_id: {selected or page_id}")
        page = self._pages[selected]
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            self._page_closed(selected)
            raise ValueError(f"page is closed: {selected}")
        self._active_page_id = selected
        return page

    def page_id(self, page: Any | None = None) -> str:
        if page is None:
            if self._active_page_id is None:
                return "main"
            return self._active_page_id
        return self._page_ids.get(id(page), self._active_page_id or "main")

    async def close(self) -> None:
        async with self._lock:
            if self._pending_tasks:
                await asyncio.gather(*tuple(self._pending_tasks), return_exceptions=True)
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._playwright = self._browser = self._context = None
            self._pages.clear()
            self._page_ids.clear()
            self._active_page_id = None

    async def context(self):
        await self._ensure_started()
        return self._context

    def validate_url(self, url: str) -> str:
        origin = _origin(url)
        if self.allowed_origins and origin not in self.allowed_origins:
            raise ValueError(f"URL origin is not allowed: {origin}")
        return url

    def validate_upload_paths(self, paths: list[str]) -> list[Path]:
        if not paths:
            raise ValueError("at least one upload path is required")
        resolved: list[Path] = []
        roots = (self.workdir, self.shared_dir)
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute():
                path = self.workdir / path
            try:
                target = path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"upload file not found: {Path(raw).name}") from exc
            if not target.is_file():
                raise ValueError(f"upload path is not a regular file: {Path(raw).name}")
            if not any(target == root or target.is_relative_to(root) for root in roots):
                raise ValueError(f"upload path is outside the allowed workspaces: {Path(raw).name}")
            resolved.append(target)
        return resolved

    def new_artifact_path(
        self, kind: Literal["screenshots", "downloads"], *, suffix: str
    ) -> Path:
        safe_suffix = suffix.lower() if re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix or "") else ""
        directory = self.artifact_root / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{uuid.uuid4().hex}{safe_suffix}"

    def record_artifact(
        self,
        *,
        tool: str,
        artifact_type: str,
        path: Path,
        url: str,
        request_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = path.resolve(strict=True)
        if not target.is_relative_to(self.artifact_root):
            raise ValueError("artifact path escaped the configured artifact root")
        size = target.stat().st_size
        if size > self.artifact_max_bytes:
            target.unlink(missing_ok=True)
            raise ValueError(
                f"artifact exceeds the {self.artifact_max_bytes}-byte per-file limit"
            )
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        relative_path = target.relative_to(self.artifact_root).as_posix()
        artifact_id = f"{artifact_type}:{target.stem}"
        metadata = {
            "timestamp": _utcnow(),
            "project_id": self.project_id,
            "member": self.member,
            "tool": tool,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "size": size,
            "sha256": digest.hexdigest(),
            "url": _redact_url(url),
            "request_summary": _redact_data(request_summary),
        }
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with (self.artifact_root / "metadata.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
        return {
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "path": str(target),
            "size": size,
            "sha256": digest.hexdigest(),
        }

    def _event(self, page_id: str, **values: Any) -> dict[str, Any]:
        event = {
            "event_id": self._next_event_id,
            "timestamp": _utcnow(),
            "page_id": page_id,
            **values,
        }
        self._next_event_id += 1
        return event

    def _request_page_id(self, request: Any) -> str:
        try:
            return self.page_id(request.frame.page)
        except Exception:
            return self._active_page_id or "main"

    def _network_request_allowed(self, request: Any) -> bool:
        if not self._network_active:
            return False
        page_id = self._request_page_id(request)
        if self._network_page_id and page_id != self._network_page_id:
            return False
        resource_type = str(getattr(request, "resource_type", "") or "")
        return self._network_include_resources or resource_type in _DEFAULT_RESOURCE_TYPES

    def _on_request(self, request: Any) -> None:
        if self._network_request_allowed(request):
            self._request_started[id(request)] = time.monotonic()

    def _spawn(self, coroutine) -> None:
        try:
            task = asyncio.create_task(coroutine)
        except RuntimeError:
            return
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _on_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        if request is not None and self._network_request_allowed(request):
            self._spawn(self._record_response(response))

    async def _record_response(self, response: Any) -> None:
        request = response.request
        page_id = self._request_page_id(request)
        try:
            headers = await response.all_headers()
        except Exception:
            headers = {}
        content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
        preview = None
        preview_truncated = False
        if self._network_capture_preview and any(
            content_type.startswith(prefix) for prefix in _TEXT_CONTENT_TYPES
        ):
            try:
                body = await response.body()
                preview_truncated = len(body) > self._network_preview_limit
                preview = _redact_text(
                    body[: self._network_preview_limit].decode("utf-8", errors="replace"),
                    self._network_preview_limit,
                )
            except Exception:
                preview = None
        started = self._request_started.pop(id(request), None)
        redirected_from = getattr(request, "redirected_from", None)
        event = self._event(
            page_id,
            type="response",
            method=str(getattr(request, "method", "GET")),
            url=_redact_url(str(getattr(request, "url", ""))),
            resource_type=str(getattr(request, "resource_type", "")),
            status=int(getattr(response, "status", 0) or 0),
            content_type=content_type,
            duration_ms=(round((time.monotonic() - started) * 1000, 1) if started else None),
            failure=None,
            redirected_from=(
                _redact_url(str(getattr(redirected_from, "url", "")))
                if redirected_from is not None else None
            ),
        )
        if preview is not None:
            event["response_preview"] = preview
            event["preview_truncated"] = preview_truncated
        self._network_events.append(event)

    def _on_request_failed(self, request: Any) -> None:
        if not self._network_request_allowed(request):
            return
        started = self._request_started.pop(id(request), None)
        failure = getattr(request, "failure", None)
        if isinstance(failure, dict):
            failure = failure.get("errorText") or failure.get("error_text")
        redirected_from = getattr(request, "redirected_from", None)
        self._network_events.append(
            self._event(
                self._request_page_id(request),
                type="request_failed",
                method=str(getattr(request, "method", "GET")),
                url=_redact_url(str(getattr(request, "url", ""))),
                resource_type=str(getattr(request, "resource_type", "")),
                status=None,
                content_type="",
                duration_ms=(round((time.monotonic() - started) * 1000, 1) if started else None),
                failure=_redact_text(failure),
                redirected_from=(
                    _redact_url(str(getattr(redirected_from, "url", "")))
                    if redirected_from is not None else None
                ),
            )
        )

    def _record_console(self, page_id: str, message: Any) -> None:
        location = getattr(message, "location", None)
        location = _redact_data(location) if isinstance(location, dict) else None
        self._console_events.append(
            self._event(
                page_id,
                level=str(getattr(message, "type", "log")),
                text=_redact_text(getattr(message, "text", message)),
                location=location,
            )
        )

    def _record_page_error(self, page_id: str, error: Any) -> None:
        self._page_errors.append(
            self._event(
                page_id,
                name=str(getattr(error, "name", type(error).__name__))[:200],
                message=_redact_text(getattr(error, "message", error)),
                stack=_redact_text(getattr(error, "stack", ""), 8000),
            )
        )

    async def network_log_start(
        self,
        *,
        include_resources: bool,
        capture_response_preview: bool,
        preview_limit: int,
        page_id: str | None,
    ) -> dict[str, Any]:
        page = await self.page(page_id)
        selected = self.page_id(page)
        self._network_events.clear()
        self._request_started.clear()
        self._network_include_resources = include_resources
        self._network_capture_preview = capture_response_preview
        self._network_preview_limit = min(max(1, preview_limit), self.response_preview_bytes)
        self._network_page_id = selected if page_id else None
        self._network_active = True
        return {
            "page_id": selected,
            "include_resources": include_resources,
            "capture_response_preview": capture_response_preview,
            "preview_limit": self._network_preview_limit,
        }

    async def network_log_list(
        self,
        *,
        after_id: int | None,
        limit: int,
        url_contains: str | None,
        methods: list[str] | None,
        statuses: list[int] | None,
    ) -> dict[str, Any]:
        if self._pending_tasks:
            await asyncio.gather(*tuple(self._pending_tasks), return_exceptions=True)
        events = self._network_events.list(after_id=after_id)
        method_set = {method.upper() for method in methods or []}
        status_set = set(statuses or [])
        if url_contains:
            events = [event for event in events if url_contains in event["url"]]
        if method_set:
            events = [event for event in events if event["method"].upper() in method_set]
        if status_set:
            events = [event for event in events if event["status"] in status_set]
        events = events[: min(max(1, limit), 200)]
        next_after_id = events[-1]["event_id"] if events else (after_id or 0)
        return {
            "events": events,
            "next_after_id": next_after_id,
            "dropped_count": self._network_events.dropped_count,
            "recording": self._network_active,
        }

    async def network_log_stop(self) -> dict[str, Any]:
        self._network_active = False
        if self._pending_tasks:
            await asyncio.gather(*tuple(self._pending_tasks), return_exceptions=True)
        self._request_started.clear()
        return {
            "recording": False,
            "events_retained": len(self._network_events.items),
            "dropped_count": self._network_events.dropped_count,
        }

    def console_logs(
        self, *, after_id: int | None, limit: int, levels: list[str] | None
    ) -> dict[str, Any]:
        events = self._console_events.list(after_id=after_id)
        level_set = {level.lower() for level in levels or []}
        if level_set:
            events = [event for event in events if event["level"].lower() in level_set]
        events = events[: min(max(1, limit), 200)]
        return {
            "events": events,
            "next_after_id": events[-1]["event_id"] if events else (after_id or 0),
            "dropped_count": self._console_events.dropped_count,
        }

    def page_errors(self, *, after_id: int | None, limit: int) -> dict[str, Any]:
        events = self._page_errors.list(after_id=after_id)[: min(max(1, limit), 200)]
        return {
            "events": events,
            "next_after_id": events[-1]["event_id"] if events else (after_id or 0),
            "dropped_count": self._page_errors.dropped_count,
        }

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
    return {"available": False, "tool": tool, "error": _redact_text(detail), **extra}


async def _browser_page(browser: Any, page_id: str | None = None):
    if page_id is None:
        return await browser.page()
    return await browser.page(page_id)


def _browser_page_id(browser: Any, page: Any) -> str:
    resolver = getattr(browser, "page_id", None)
    return str(resolver(page)) if callable(resolver) else "main"


async def _page_snapshot(page, *, max_chars: int = 20000) -> dict[str, Any]:
    title = await page.title()
    text = await page.locator("body").inner_text()
    return {"url": page.url, "final_url": page.url, "title": title, "text": text[:max_chars]}


def _close_browser_at_exit(browser: _BrowserSession) -> None:
    if getattr(browser, "_playwright", None) is None:
        return
    try:
        asyncio.run(browser.close())
    except Exception:
        pass


def build_browser_mcp(browser: _BrowserSession | None = None) -> MCPServer:
    browser = browser or _BrowserSession()

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            close = getattr(browser, "close", None)
            if callable(close):
                await close()

    server = create_mcp_server(
        "browser", "Stateful Playwright browser tools", lifespan=lifespan
    )
    atexit.register(_close_browser_at_exit, browser)

    @server.tool(
        name="navigate",
        description="Render a URL and return status, final URL, title, and visible text.",
    )
    async def navigate(
        url: str,
        wait_until: str = "load",
        timeout_ms: int = 30000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            validator = getattr(browser, "validate_url", None)
            if callable(validator):
                validator(url)
            page = await _browser_page(browser, page_id)
            selected = _browser_page_id(browser, page)
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            snapshot = await _page_snapshot(page)
            content_type = ""
            status = None
            javascript_redirect = False
            if response is not None:
                status = response.status
                content_type = (await response.all_headers()).get("content-type", "")
                javascript_redirect = page.url != url and response.request.redirected_from is None
                if content_type and "html" not in content_type and "text" not in content_type:
                    body = (await response.body()).decode(errors="replace")
                    title, text = _html_summary(body)
                    snapshot.update({"title": title, "text": text or body[:20000]})
            return {
                "available": True,
                "page_id": selected,
                "input_url": url,
                "status": status,
                "content_type": content_type,
                "javascript_redirect": javascript_redirect,
                **snapshot,
            }
        except Exception as exc:
            return _tool_unavailable("browser.navigate", str(exc), url=_redact_url(url), page_id=page_id)

    @server.tool(name="click", description="Click an element and return the resulting page state.")
    async def click(selector: str, page_id: str | None = None) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            await page.click(selector)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "selector": selector,
                **await _page_snapshot(page),
            }
        except Exception as exc:
            return _tool_unavailable("browser.click", str(exc), selector=selector)

    @server.tool(name="fill", description="Fill an input or textarea without submitting it.")
    async def fill(selector: str, value: str, page_id: str | None = None) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            await page.fill(selector, value)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "selector": selector,
                "value_length": len(value),
                "url": page.url,
            }
        except Exception as exc:
            return _tool_unavailable("browser.fill", str(exc), selector=selector)

    @server.tool(name="press", description="Press a keyboard key on the current page.")
    async def press(key: str, page_id: str | None = None) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            await page.keyboard.press(key)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "key": key,
                **await _page_snapshot(page),
            }
        except Exception as exc:
            return _tool_unavailable("browser.press", str(exc), key=key)

    @server.tool(name="eval_js", description="Evaluate JavaScript in the current page context.")
    async def eval_js(script: str, page_id: str | None = None) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "result": await page.evaluate(script),
                "url": page.url,
            }
        except Exception as exc:
            return _tool_unavailable("browser.eval_js", str(exc))

    @server.tool(name="get_content", description="Return rendered HTML and visible text for the current page.")
    async def get_content(page_id: str | None = None) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "html": await page.content(),
                "text": await page.locator("body").inner_text(),
            }
        except Exception as exc:
            return _tool_unavailable("browser.get_content", str(exc))

    @server.tool(
        name="screenshot",
        description="Capture the current rendered page with Playwright.",
    )
    async def screenshot(
        path: str | None = None,
        full_page: bool = True,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        artifact_path = getattr(browser, "new_artifact_path", None)
        if callable(artifact_path):
            if path and Path(path).name != path:
                return _tool_unavailable(
                    "browser.screenshot", "path must be a file name without directories", path=path
                )
            out = artifact_path("screenshots", suffix=".png")
        else:
            out = Path(path) if path else Path.cwd() / f"browser_{int(time.time() * 1000)}.png"
        try:
            page = await _browser_page(browser, page_id)
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=full_page)
            result = {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "path": str(out),
            }
            recorder = getattr(browser, "record_artifact", None)
            if callable(recorder):
                result.update(
                    recorder(
                        tool="screenshot",
                        artifact_type="screenshot",
                        path=out,
                        url=page.url,
                    )
                )
            return result
        except Exception as exc:
            return _tool_unavailable("browser.screenshot", str(exc), path=str(out))

    @server.tool(
        name="cookies",
        description="Return cookies from the persistent browser context; values are redacted by default.",
    )
    async def cookies(
        include_values: bool = False,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            context = await browser.context()
            items = await context.cookies()
            if not include_values:
                items = [
                    {
                        **{key: value for key, value in cookie.items() if key != "value"},
                        "value": "[REDACTED]",
                        "value_length": len(str(cookie.get("value", ""))),
                    }
                    for cookie in items
                ]
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "sensitive_values_included": include_values,
                "cookies": items,
            }
        except Exception as exc:
            return _tool_unavailable("browser.cookies", str(exc))

    @server.tool(name="set_cookie", description="Add or replace a cookie in the persistent browser context.")
    async def set_cookie(
        name: str,
        value: str,
        url: str | None = None,
        domain: str | None = None,
        path: str = "/",
        http_only: bool = False,
        secure: bool = False,
        same_site: str = "Lax",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            page = await _browser_page(browser, page_id)
            context = await browser.context()
            cookie: dict[str, Any] = {
                "name": name,
                "value": value,
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": same_site.capitalize(),
            }
            if url:
                cookie["url"] = url
            elif domain:
                cookie["domain"] = domain
                cookie["path"] = path
            else:
                cookie["url"] = page.url
            await context.add_cookies([cookie])
            public_cookie = {key: val for key, val in cookie.items() if key != "value"}
            public_cookie.update({"value": "[REDACTED]", "value_length": len(value)})
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "cookie": public_cookie,
            }
        except Exception as exc:
            return _tool_unavailable("browser.set_cookie", str(exc), name=name)

    @server.tool(
        name="wait_for",
        description="Wait for a selector, URL, or page load state and return a bounded page snapshot.",
    )
    async def wait_for(
        selector: str | None = None,
        url: str | None = None,
        state: Literal["attached", "detached", "visible", "hidden"] = "visible",
        load_state: Literal["domcontentloaded", "load", "networkidle"] | None = None,
        timeout_ms: int = 10000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            if not any((selector, url, load_state)):
                raise ValueError("at least one of selector, url, or load_state is required")
            if timeout_ms <= 0:
                raise ValueError("timeout_ms must be positive")
            page = await _browser_page(browser, page_id)
            if selector:
                await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            if url:
                await page.wait_for_url(url, timeout=timeout_ms)
            if load_state:
                await page.wait_for_load_state(load_state, timeout=timeout_ms)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "waited_for": {
                    "selector": selector,
                    "url": url,
                    "state": state if selector else None,
                    "load_state": load_state,
                },
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                **await _page_snapshot(page, max_chars=6000),
            }
        except Exception as exc:
            return _tool_unavailable("browser.wait_for", str(exc), page_id=page_id)

    @server.tool(name="network_log_start", description="Start bounded browser network recording.")
    async def network_log_start(
        include_resources: bool = False,
        capture_response_preview: bool = False,
        preview_limit: int = 4096,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            if preview_limit <= 0:
                raise ValueError("preview_limit must be positive")
            result = await browser.network_log_start(
                include_resources=include_resources,
                capture_response_preview=capture_response_preview,
                preview_limit=preview_limit,
                page_id=page_id,
            )
            return {"available": True, **result}
        except Exception as exc:
            return _tool_unavailable("browser.network_log_start", str(exc), page_id=page_id)

    @server.tool(name="network_log_list", description="List bounded, redacted network events incrementally.")
    async def network_log_list(
        after_id: int | None = None,
        limit: int = 50,
        url_contains: str | None = None,
        methods: list[str] | None = None,
        statuses: list[int] | None = None,
    ) -> dict[str, Any]:
        try:
            return {
                "available": True,
                **await browser.network_log_list(
                    after_id=after_id,
                    limit=limit,
                    url_contains=url_contains,
                    methods=methods,
                    statuses=statuses,
                ),
            }
        except Exception as exc:
            return _tool_unavailable("browser.network_log_list", str(exc))

    @server.tool(name="network_log_stop", description="Stop browser network recording.")
    async def network_log_stop() -> dict[str, Any]:
        try:
            return {"available": True, **await browser.network_log_stop()}
        except Exception as exc:
            return _tool_unavailable("browser.network_log_stop", str(exc))

    @server.tool(name="console_logs", description="List bounded, redacted browser console events.")
    async def console_logs(
        after_id: int | None = None,
        limit: int = 50,
        levels: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            page = await _browser_page(browser)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                **browser.console_logs(after_id=after_id, limit=limit, levels=levels),
            }
        except Exception as exc:
            return _tool_unavailable("browser.console_logs", str(exc))

    @server.tool(name="page_errors", description="List bounded, redacted uncaught page errors.")
    async def page_errors(
        after_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            page = await _browser_page(browser)
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                **browser.page_errors(after_id=after_id, limit=limit),
            }
        except Exception as exc:
            return _tool_unavailable("browser.page_errors", str(exc))

    @server.tool(
        name="upload_file",
        description="Upload files from the Member or shared workspace into a file input.",
    )
    async def upload_file(
        selector: str,
        paths: list[str],
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            resolved = browser.validate_upload_paths(paths)
            page = await _browser_page(browser, page_id)
            await page.locator(selector).set_input_files([str(path) for path in resolved])
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "selector": selector,
                "files": [
                    {"name": path.name, "size": path.stat().st_size} for path in resolved
                ],
            }
        except Exception as exc:
            return _tool_unavailable("browser.upload_file", str(exc), selector=selector)

    @server.tool(
        name="download",
        description="Click a download control or navigate to a download URL and save an artifact.",
    )
    async def download(
        selector: str | None = None,
        url: str | None = None,
        timeout_ms: int = 30000,
        page_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            if bool(selector) == bool(url):
                raise ValueError("exactly one of selector or url is required")
            if timeout_ms <= 0:
                raise ValueError("timeout_ms must be positive")
            if url:
                validator = getattr(browser, "validate_url", None)
                if callable(validator):
                    validator(url)
            page = await _browser_page(browser, page_id)
            async with page.expect_download(timeout=timeout_ms) as download_info:
                if selector:
                    await page.locator(selector).click()
                else:
                    try:
                        await page.goto(url, wait_until="commit", timeout=timeout_ms)
                    except Exception:
                        # Chromium aborts the navigation when a response becomes a
                        # download. The enclosing download expectation is the
                        # authoritative success/failure signal.
                        pass
            item = await download_info.value
            suggested_name = Path(str(getattr(item, "suggested_filename", "download"))).name
            suffix = Path(suggested_name).suffix
            out = browser.new_artifact_path("downloads", suffix=suffix)
            await item.save_as(str(out))
            artifact = browser.record_artifact(
                tool="download",
                artifact_type="download",
                path=out,
                url=url or page.url,
                request_summary={
                    "method": "GET" if url else "CLICK",
                    "url": _redact_url(url or page.url),
                },
            )
            return {
                "available": True,
                "page_id": _browser_page_id(browser, page),
                "url": page.url,
                "suggested_filename": suggested_name,
                **artifact,
            }
        except Exception as exc:
            return _tool_unavailable("browser.download", str(exc), page_id=page_id)

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


def _spider(url: str) -> dict[str, Any]:
    try:
        scan = _zap_get("/JSON/spider/action/scan/", url=url)
        scan_id = scan.get("scan")
        urls = _zap_get("/JSON/core/view/urls/", baseurl=url).get("urls", [])
    except (requests.RequestException, ValueError) as exc:
        return _tool_unavailable("zap.spider", str(exc), url=url, urls_found=[])
    return {"available": True, "url": url, "scan": scan_id, "urls_found": urls}


def _active_scan(url: str) -> dict[str, Any]:
    try:
        scan = _zap_get("/JSON/ascan/action/scan/", url=url)
        alerts = _zap_get("/JSON/core/view/alerts/", baseurl=url).get("alerts", [])
    except (requests.RequestException, ValueError) as exc:
        return _tool_unavailable("zap.active_scan", str(exc), url=url, alerts=[])
    return {"available": True, "url": url, "scan": scan.get("scan"), "alerts": alerts}


def build_zap_mcp() -> MCPServer:
    server = create_mcp_server("zap", "OWASP ZAP API adapter")

    @server.tool(
        name="spider",
        description="Run ZAP spider against a target URL and return discovered URLs.",
    )
    async def spider(url: str) -> dict[str, Any]:
        return await asyncio.to_thread(_spider, url)

    @server.tool(
        name="active_scan",
        description="Run a ZAP active scan against a target and return current alerts.",
    )
    async def active_scan(url: str) -> dict[str, Any]:
        return await asyncio.to_thread(_active_scan, url)

    return server
