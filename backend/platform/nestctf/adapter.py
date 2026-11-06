"""Native adapter for the nestctf (强网拟态) match platform.

The participant console is a single-page app over a JSON API on the same origin:

    GET  /api/ct/web/jeopardy_race/race/<race>/checkpoints/?direction=
    GET  /api/ct/web/jeopardy_race/race/<race>/checkpoints/<resource_id>/
    POST /api/ct/web/jeopardy_race/race/<race>/flag/

Every call carries the participant JWT in an Authorization header.  The list
endpoint only knows the score board (name, score, solved count); the
description, the attachment and - the field this adapter exists for - the
with_scene flag only appear on the per-checkpoint detail endpoint.

with_scene separates a challenge that needs a container from one solved from a
handout, so the adapter maps it onto PlatformChallenge.remote: an
attachments_only workflow therefore imports exactly the challenges that need
no team container.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import urljoin

from backend.platform._common import PLATFORM_CATEGORIES
from backend.platform.adapter import HttpJsonAdapter
from backend.platform.errors import PlatformError
from backend.platform.mapping import FieldMapping, PlatformChallenge

RACE_PATH = "/api/ct/web/jeopardy_race/race/{race_id}"

#: Categories the console labels differently from IPC's vocabulary.
_CATEGORY_ALIASES = {
    "misc": "misc",
    "web": "web",
    "pwn": "pwn",
    "reverse": "reverse",
    "crypto": "crypto",
    "ai": "ai",
    "blockchain": "misc",
    "osint": "osint",
    "pentest": "pentest",
}

_TAG = re.compile(r"<[^>]+>")


class NestctfError(PlatformError):
    """A nestctf API call failed."""


def _plain_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", "\n", raw, flags=re.IGNORECASE)
    return html.unescape(_TAG.sub("", text)).strip()


class NestctfClient:
    """JWT-authenticated participant client for one nestctf race."""

    def __init__(
        self,
        base_url: str,
        race_id: str,
        token: str,
        *,
        timeout: float = 30,
        request_get: Any = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.race_id = (race_id or "").strip()
        self.token = (token or "").strip()
        self.timeout = timeout
        self._request_get = request_get
        self.headers = {"Authorization": "JWT " + self.token} if self.token else {}

    def get(self, url: str, **kwargs: Any):
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", self.timeout)
        if self._request_get is not None:
            return self._request_get(url, **kwargs)
        import requests

        return requests.get(url, **kwargs)

    def _get(self, path: str) -> Any:
        response = self.get(self.base_url + path)
        response.raise_for_status()
        try:
            envelope = response.json()
        except ValueError as exc:
            raise NestctfError("nestctf did not return JSON") from exc
        if not isinstance(envelope, dict):
            raise NestctfError("nestctf returned an unexpected envelope")
        code = envelope.get("code")
        # The console answers AD-000000 on success and AD-xxxxxx on failure.
        if code not in (0, 200, None, "AD-000000"):
            raise NestctfError(
                "nestctf " + path + " failed: " + str(envelope.get("message") or code)
            )
        return envelope.get("data")

    def checkpoints(self) -> list[dict[str, Any]]:
        data = self._get(
            RACE_PATH.format(race_id=self.race_id) + "/checkpoints/?direction="
        )
        items = data.get("list") if isinstance(data, dict) else data
        return [item for item in items or [] if isinstance(item, dict)]

    def checkpoint(self, resource_id: str) -> dict[str, Any]:
        data = self._get(
            RACE_PATH.format(race_id=self.race_id) + "/checkpoints/" + str(resource_id) + "/"
        )
        return data if isinstance(data, dict) else {}


class NestctfAdapter(HttpJsonAdapter):
    """Map the nestctf console onto IPC's PlatformAdapter contract."""

    #: Scene flags, descriptions and handouts never change during a race.
    DETAIL_TTL_SECONDS = 900.0

    def __init__(
        self,
        client: NestctfClient,
        mapping: FieldMapping,
        *,
        max_attachment_bytes: int | None = None,
    ) -> None:
        super().__init__(
            mapping, request_get=client.get, max_attachment_bytes=max_attachment_bytes
        )
        self.client = client
        self.mapping = mapping
        self.attachments_only = bool(getattr(mapping, "attachments_only", False))
        self.exclude = {
            str(value or "").strip().casefold()
            for value in (getattr(mapping, "exclude", None) or [])
            if str(value or "").strip()
        }
        self._details: dict[str, tuple[float, dict[str, Any]]] = {}
        self._by_id: dict[str, PlatformChallenge] = {}

    # -- challenges -----------------------------------------------------

    def _category(self, raw: str) -> str:
        key = str(raw or "").strip().lower()
        mapped = self.mapping.category_map.get(raw) or self.mapping.category_map.get(key)
        if mapped and mapped in PLATFORM_CATEGORIES:
            return mapped
        for alias, category in _CATEGORY_ALIASES.items():
            if alias and alias in key:
                return category
        return "misc"

    def _detail(self, external_id: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._details.get(external_id)
        if cached is not None and now - cached[0] < self.DETAIL_TTL_SECONDS:
            return cached[1]
        detail = self.client.checkpoint(external_id)
        self._details[external_id] = (now, detail)
        return detail

    def _attachment_urls(self, detail: dict[str, Any]) -> list[str]:
        raw = detail.get("attachment") or detail.get("attachments") or []
        entries: list[Any] = raw if isinstance(raw, list) else [raw]
        urls: list[str] = []
        for entry in entries:
            value = ""
            if isinstance(entry, str):
                value = entry
            elif isinstance(entry, dict):
                value = str(entry.get("url") or entry.get("download_url") or "")
            if value:
                urls.append(urljoin(self.client.base_url + "/", value))
        return urls

    def fetch_challenges(self) -> list[PlatformChallenge]:
        challenges: list[PlatformChallenge] = []
        for item in self.client.checkpoints():
            external = str(item.get("resource_id") or "").strip()
            title = str(item.get("name") or "").strip()
            if not external or not title:
                continue
            if self.exclude and (
                external.casefold() in self.exclude or title.casefold() in self.exclude
            ):
                continue
            detail = self._detail(external)
            remote = bool(detail.get("with_scene"))
            if self.attachments_only and remote:
                continue
            challenges.append(
                PlatformChallenge(
                    external_id=external,
                    title=title,
                    category=self._category(
                        str(item.get("direction") or detail.get("direction") or "")
                    ),
                    description=_plain_text(str(detail.get("desc") or "")),
                    attachment_urls=self._attachment_urls(detail),
                    remote=remote,
                    solved=bool(item.get("is_solved")),
                    platform_data={
                        "score": item.get("score"),
                        "solves": item.get("solved"),
                        "with_scene": remote,
                    },
                )
            )
        # Easiest target first, the same policy the other adapters use.
        challenges.sort(
            key=lambda challenge: -int(challenge.platform_data.get("solves") or 0)
        )
        self._by_id = {challenge.external_id: challenge for challenge in challenges}
        return challenges

    # -- instances ------------------------------------------------------

    @property
    def supports_instances(self) -> bool:
        # Scene instances are created by the browser session only; the API
        # answers "no permission" for a participant token, so IPC never tries.
        return False

    def instance_limit(self) -> int | None:
        return None


__all__ = ["RACE_PATH", "NestctfAdapter", "NestctfClient", "NestctfError"]
