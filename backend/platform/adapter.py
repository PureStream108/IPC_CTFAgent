from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests

from backend.core.config import CATEGORIES
from backend.filename_util import numbered_filename, safe_stem
from backend.platform.mapping import FieldMapping, PlatformChallenge


def _json_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
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
        url = self.mapping.list_url
        params: dict[str, Any] | None = None
        pages: list[PlatformChallenge] = []
        seen_urls: set[str] = set()
        for _page in range(self.mapping.max_pages):
            if not url or url in seen_urls:
                break
            seen_urls.add(url)
            request_kwargs: dict[str, Any] = {
                "headers": self.mapping.headers,
                "timeout": self.timeout,
            }
            if params:
                request_kwargs["params"] = params
            response = self._request_get(url, **request_kwargs)
            response.raise_for_status()
            try:
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise ValueError("challenge list endpoint did not return JSON") from exc
            items = _json_path(payload, self.mapping.list_path)
            if not isinstance(items, list):
                raise ValueError(f"list_path '{self.mapping.list_path}' did not resolve to a list")
            pages.extend(self._normalize(item) for item in items)
            if not self.mapping.pagination_path:
                break
            next_url = _json_path(payload, self.mapping.pagination_path)
            if next_url in (None, ""):
                break
            if not isinstance(next_url, str):
                raise ValueError("pagination_path must resolve to a URL or null")
            next_url = urljoin(url, next_url)
            parsed = urlparse(next_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("pagination URL must use http or https")
            url = next_url
            params = None
        else:
            raise ValueError(f"challenge pagination exceeded max_pages={self.mapping.max_pages}")
        return pages

    def _normalize(self, item: Any) -> PlatformChallenge:
        if not isinstance(item, dict):
            raise ValueError("challenge list entries must be JSON objects")
        external_id = _field(item, self.mapping.id_field)
        title = _field(item, self.mapping.title_field)
        if external_id is None or title is None:
            raise ValueError("challenge is missing its configured id or title field")
        raw_category = str(_field(item, self.mapping.category_field, "misc"))
        mapped_category = self.mapping.category_map.get(raw_category, raw_category).lower()
        category = mapped_category if mapped_category in CATEGORIES else "misc"
        raw_attachments = _field(item, self.mapping.attachments_field, []) or []
        if isinstance(raw_attachments, str):
            raw_attachments = [raw_attachments]
        if not isinstance(raw_attachments, list):
            raise ValueError("configured attachments field must contain a URL or list of URLs")
        base_url = self.mapping.attachment_base_url or self.mapping.list_url
        attachment_urls = [urljoin(base_url, str(url)) for url in raw_attachments]
        return PlatformChallenge(
            external_id=str(external_id),
            title=str(title),
            category=category,
            description=str(_field(item, self.mapping.description_field, "") or ""),
            attachment_urls=attachment_urls,
            remote=bool(self.mapping.remote_field and _field(item, self.mapping.remote_field, False) is True),
            solved=bool(self.mapping.solved_field and _field(item, self.mapping.solved_field, False) is True),
            hints=([str(h) for h in _field(item, self.mapping.hints_field, [])]
                   if self.mapping.hints_field and isinstance(_field(item, self.mapping.hints_field, []), list) else []),
        )

    def download_attachments(
        self,
        challenge: PlatformChallenge,
        dest_dir: str | Path,
    ) -> list[Path]:
        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
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
            response = self._request_get(
                url,
                headers=self.mapping.headers,
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            written = 0
            try:
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if written == 0:
                            content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
                            sample = bytes(chunk[:256]).lstrip().lower()
                            if "text/html" in content_type or sample.startswith((b"<!doctype html", b"<html", b"<head")):
                                raise ValueError("attachment endpoint returned an HTML/authentication page")
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
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            downloaded.append(target)
        return downloaded
