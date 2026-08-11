from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
import re
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests

from backend.core.config import CATEGORIES
from backend.filename_util import numbered_filename, safe_stem
from backend.platform.mapping import FieldMapping, PlatformChallenge

_MAX_EXTERNAL_ID_LENGTH = 512
_MAX_TITLE_LENGTH = 1_000
_MAX_ATTACHMENTS_PER_CHALLENGE = 256
_ATTACHMENT_URL_KEYS = ("url", "download_url", "download", "href", "path", "file_url")
_ATTACHMENT_METADATA_KEYS = {"name", "filename", "size", "mime", "mime_type", "content_type"}


def _json_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
            continue
        raise ValueError(f"JSON path not found: {path}")
    return current


def _field(item: dict[str, Any], path: str, default: Any = None) -> Any:
    try:
        return _json_path(item, path)
    except ValueError:
        return default


class PlatformAdapter(ABC):
    @abstractmethod
    def fetch_challenges(self) -> list[PlatformChallenge]: ...

    @abstractmethod
    def download_attachments(
        self,
        challenge: PlatformChallenge,
        dest_dir: str | Path,
    ) -> list[Path]: ...


class HttpJsonAdapter(PlatformAdapter):
    def __init__(
        self,
        mapping: FieldMapping,
        *,
        timeout: float = 30,
        request_get: Callable[..., Any] | None = None,
        max_attachment_bytes: int | None = None,
    ) -> None:
        self.mapping = mapping
        self.timeout = timeout
        self._request_get = request_get or requests.get
        self.max_attachment_bytes = max_attachment_bytes

    def fetch_challenges(self) -> list[PlatformChallenge]:
        response = self._request_get(
            self.mapping.list_url,
            headers=self.mapping.headers,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            _close_response(response)
        items = _json_path(payload, self.mapping.list_path)
        if not isinstance(items, list):
            raise ValueError(f"list_path '{self.mapping.list_path}' did not resolve to a list")
        return [self._normalize(item) for item in items]

    def _normalize(self, item: Any) -> PlatformChallenge:
        if not isinstance(item, dict):
            raise ValueError("challenge list entries must be JSON objects")
        raw_external_id = _field(item, self.mapping.id_field)
        raw_title = _field(item, self.mapping.title_field)
        if raw_external_id is None or raw_title is None:
            raise ValueError("challenge is missing its configured id or title field")
        external_id = str(raw_external_id).strip()
        title = str(raw_title).strip()
        if not external_id or not title:
            raise ValueError("challenge id and title must be non-empty")
        if len(external_id) > _MAX_EXTERNAL_ID_LENGTH:
            raise ValueError(f"challenge id is limited to {_MAX_EXTERNAL_ID_LENGTH} characters")
        if len(title) > _MAX_TITLE_LENGTH:
            raise ValueError(f"challenge title is limited to {_MAX_TITLE_LENGTH} characters")
        raw_category = str(_field(item, self.mapping.category_field, "misc"))
        mapped_category = self.mapping.category_map.get(raw_category, raw_category).lower()
        category = mapped_category if mapped_category in CATEGORIES else "misc"
        raw_attachments = _field(item, self.mapping.attachments_field, []) or []
        attachment_refs = _attachment_references(raw_attachments)
        if len(attachment_refs) > _MAX_ATTACHMENTS_PER_CHALLENGE:
            raise ValueError(
                f"challenge attachments are limited to {_MAX_ATTACHMENTS_PER_CHALLENGE} entries"
            )
        base_url = self.mapping.attachment_base_url or self.mapping.list_url
        attachment_urls = [urljoin(base_url, reference) for reference in attachment_refs]
        return PlatformChallenge(
            external_id=external_id,
            title=title,
            category=category,
            description=str(_field(item, self.mapping.description_field, "") or ""),
            attachment_urls=attachment_urls,
        )

    def download_attachments(
        self,
        challenge: PlatformChallenge,
        dest_dir: str | Path,
    ) -> list[Path]:
        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        try:
            for index, url in enumerate(challenge.attachment_urls, start=1):
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"}:
                    raise ValueError(f"attachment URL must use http or https: {url}")
                source_name = Path(unquote(parsed.path)).name or f"attachment-{index}"
                source_path = Path(source_name)
                stem = safe_stem(source_path.stem, fallback=f"attachment-{index}")
                suffix = source_path.suffix[:20]
                filename = numbered_filename(
                    stem,
                    suffix or ".bin",
                    [path.name for path in destination.iterdir()],
                    fallback=f"attachment-{index}",
                )
                target = destination / filename
                attachment_headers = self.mapping.attachment_headers
                if not attachment_headers and _same_origin(url, self.mapping.list_url):
                    attachment_headers = self.mapping.headers
                response = self._request_get(
                    url,
                    headers=attachment_headers,
                    timeout=self.timeout,
                    stream=True,
                )
                written = 0
                try:
                    response.raise_for_status()
                    content_length = _content_length(response)
                    if (
                        self.max_attachment_bytes is not None
                        and content_length is not None
                        and content_length > self.max_attachment_bytes
                    ):
                        raise ValueError(
                            f"attachment exceeds {self.max_attachment_bytes} byte limit: {url}"
                        )
                    with target.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if self.max_attachment_bytes is not None and written > self.max_attachment_bytes:
                                raise ValueError(
                                    f"attachment exceeds {self.max_attachment_bytes} byte limit: {url}"
                                )
                            handle.write(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                finally:
                    _close_response(response)
                downloaded.append(target)
        except Exception:
            for path in downloaded:
                path.unlink(missing_ok=True)
            raise
        return downloaded


def _attachment_references(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 4:
        raise ValueError("configured attachments field is nested too deeply")
    if value is None:
        return []
    if isinstance(value, str):
        reference = value.strip()
        return [reference] if reference else []
    if isinstance(value, (list, tuple)):
        references: list[str] = []
        for item in value:
            references.extend(_attachment_references(item, depth=depth + 1))
        return references
    if isinstance(value, dict):
        normalized = {
            re.sub(r"(?<!^)(?=[A-Z])", "_", str(key).strip())
            .lower()
            .replace("-", "_")
            .replace(" ", "_"): item
            for key, item in value.items()
        }
        for key in _ATTACHMENT_URL_KEYS:
            if key in normalized:
                return _attachment_references(normalized[key], depth=depth + 1)
        if _ATTACHMENT_METADATA_KEYS.intersection(normalized):
            raise ValueError("attachment object is missing a url/download_url/path field")
        references: list[str] = []
        for item in value.values():
            if isinstance(item, (str, list, tuple, dict)):
                references.extend(_attachment_references(item, depth=depth + 1))
        if references:
            return references
    raise ValueError(
        "configured attachments field must contain URLs or objects with a url/download_url/path field"
    )


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("Content-Length") or headers.get("content-length")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _same_origin(left: str, right: str) -> bool:
    left_origin = _normalized_origin(left)
    right_origin = _normalized_origin(right)
    return left_origin is not None and left_origin == right_origin


def _normalized_origin(url: str) -> tuple[str, str, int] | None:
    if "\\" in url:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return None
    return scheme, hostname, port if port is not None else (443 if scheme == "https" else 80)


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
