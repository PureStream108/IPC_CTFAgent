from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
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
    def __init__(self, mapping: FieldMapping, *, timeout: float = 30) -> None:
        self.mapping = mapping
        self.timeout = timeout

    def fetch_challenges(self) -> list[PlatformChallenge]:
        response = requests.get(
            self.mapping.list_url,
            headers=self.mapping.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        items = _json_path(payload, self.mapping.list_path)
        if not isinstance(items, list):
            raise ValueError(f"list_path '{self.mapping.list_path}' did not resolve to a list")
        return [self._normalize(item) for item in items]

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
            response = requests.get(
                url,
                headers=self.mapping.headers,
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            downloaded.append(target)
        return downloaded
