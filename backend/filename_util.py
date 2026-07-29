from __future__ import annotations

from collections.abc import Iterable

_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')


def safe_stem(name: str, fallback: str = "file", max_length: int = 120) -> str:
    cleaned = "".join(
        "_" if c in _INVALID_FILENAME_CHARS or ord(c) < 32 else c
        for c in name
    )
    cleaned = cleaned.strip().rstrip(".")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip().rstrip(".")
    return cleaned or fallback


def numbered_filename(
    name: str,
    extension: str,
    used: Iterable[str],
    fallback: str = "file",
) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    base = safe_stem(name, fallback=fallback)
    # Host bind mounts may be case-insensitive even though the application
    # container is Linux. Treat names case-insensitively so an export never
    # overwrites e.g. ``Task.md`` with ``task.md``.
    used_names = {value.casefold() for value in used}
    candidate = f"{base}{ext}"
    if candidate.casefold() not in used_names:
        return candidate
    counter = 1
    while True:
        candidate = f"{base}{counter:02d}{ext}"
        if candidate.casefold() not in used_names:
            return candidate
        counter += 1
