"""Redaction helpers for user-facing competition data."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


def flag_digest(value: str | None) -> str | None:
    if value is None or not str(value):
        return None
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def redact_flag(value: str | None) -> str | None:
    return flag_digest(value)


def redact_text(value: str, secret: str | None) -> str:
    if not secret:
        return value
    digest = redact_flag(secret) or "[redacted]"
    return value.replace(secret, digest)


def redact_object(value: Any, secret: str | None) -> Any:
    """Recursively replace one known secret in JSON-compatible output."""
    if isinstance(value, str):
        return redact_text(value, secret)
    if isinstance(value, Mapping):
        return {key: redact_object(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_object(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_object(item, secret) for item in value)
    return value
