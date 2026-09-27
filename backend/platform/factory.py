"""Construct platform adapters without leaking platform branches into IPC."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from backend.platform.adapter import HttpJsonAdapter, PlatformAdapter
from backend.platform.gzctf import GZCTFAdapter, GZCTFClient
from backend.platform.mapping import FieldMapping
from backend.platform.ret2shell import Ret2ShellAdapter, Ret2ShellClient


def build_adapter(
    mapping: FieldMapping,
    *,
    max_attachment_bytes: int | None = None,
    request_get: Callable[..., Any] | None = None,
    base_url: str | None = None,
    credentials: Mapping[str, str] | None = None,
    competition_id: str = "",
    team_id: str = "",
) -> PlatformAdapter:
    """Build one isolated adapter from a validated platform mapping."""

    credentials = credentials or {}

    if mapping.platform == "ret2shell":
        return Ret2ShellAdapter(
            Ret2ShellClient(base_url=base_url or "", game_id=mapping.game_id),
            game_id=mapping.game_id,
            category_map=mapping.category_map,
            max_attachment_bytes=max_attachment_bytes,
        )
    if mapping.platform == "gzctf":
        resolved_base_url = base_url or ""
        source_url = resolved_base_url or mapping.list_url
        if source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme and parsed.netloc:
                # Standalone preview callers commonly provide the concrete
                # ``/api/Game/...`` endpoint as list_url. Native GZCTF URLs
                # are rooted at the deployment origin, so never append the
                # client API paths below an already-qualified endpoint.
                if not resolved_base_url or parsed.path.lower().startswith("/api/"):
                    resolved_base_url = f"{parsed.scheme}://{parsed.netloc}"
        return GZCTFAdapter(
            GZCTFClient(
                base_url=resolved_base_url,
                token=(credentials.get("gzctf_token") or credentials.get("platform_token") or credentials.get("token") or ""),
                username=credentials.get("gzctf_username", ""),
                password=credentials.get("gzctf_password", ""),
            ),
            mapping,
            max_attachment_bytes=max_attachment_bytes,
            competition_id=competition_id,
            team_id=team_id,
        )
    return HttpJsonAdapter(
        mapping,
        request_get=request_get or requests.get,
        max_attachment_bytes=max_attachment_bytes,
    )
