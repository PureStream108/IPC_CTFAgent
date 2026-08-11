#!/usr/bin/env python3
"""Inspect a saved platform JSON response and suggest IPC field mappings."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 8
MAX_CANDIDATES = 50
MAX_SAMPLE_ITEMS = 20

ALIASES = {
    "id_field": ("id", "challenge_id", "challengeid", "uuid", "slug"),
    "title_field": ("name", "title", "display_name", "challenge_name"),
    "category_field": ("category", "kind", "type", "track"),
    "description_field": ("description", "body", "content", "text", "statement"),
    "attachments_field": (
        "files",
        "attachments",
        "downloads",
        "download_files",
        "resources",
    ),
}


def normalize_key(value: Any) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(value).strip())
    return re.sub(r"[\s-]+", "_", text.lower())


def read_payload(source: str) -> Any:
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        path = Path(source)
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError(f"input exceeds {MAX_INPUT_BYTES} byte limit")
        raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} byte limit")
    return json.loads(raw.decode("utf-8-sig"))


def candidate_lists(value: Any) -> list[tuple[list[str], list[Any]]]:
    found: list[tuple[list[str], list[Any]]] = []
    visited: set[int] = set()

    def visit(item: Any, path: list[str], depth: int) -> None:
        if depth > MAX_DEPTH or len(found) >= MAX_CANDIDATES:
            return
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(item, list):
            if item and any(isinstance(child, dict) for child in item[:MAX_SAMPLE_ITEMS]):
                found.append((list(path), item))
            for index, child in enumerate(item[:3]):
                if isinstance(child, (dict, list)):
                    visit(child, [*path, str(index)], depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, (dict, list)):
                    visit(child, [*path, str(key)], depth + 1)

    visit(value, [], 0)
    return found


def suggest_field(items: list[Any], aliases: tuple[str, ...]) -> str | None:
    scores: dict[str, int] = {}
    original: dict[str, str] = {}
    for item in items[:MAX_SAMPLE_ITEMS]:
        if not isinstance(item, dict):
            continue
        for key in item:
            normalized = normalize_key(key)
            if normalized in aliases:
                scores[normalized] = scores.get(normalized, 0) + 1
                original.setdefault(normalized, str(key))
    if not scores:
        return None
    winner = min(scores, key=lambda key: (-scores[key], aliases.index(key)))
    return original[winner]


def inspect(payload: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for path_segments, items in candidate_lists(payload):
        list_path = ".".join(path_segments)
        object_items = [item for item in items[:MAX_SAMPLE_ITEMS] if isinstance(item, dict)]
        if not object_items:
            continue
        mapping = {
            field: suggestion
            for field, aliases in ALIASES.items()
            if (suggestion := suggest_field(object_items, aliases)) is not None
        }
        sample_keys = sorted({str(key) for item in object_items[:5] for key in item})[:50]
        warnings: list[str] = []
        if any("." in segment for segment in path_segments):
            warnings.append("a JSON key contains '.', which IPC dotted paths cannot escape")
        if "id_field" not in mapping or "title_field" not in mapping:
            warnings.append("id/title mapping needs manual confirmation")
        candidates.append(
            {
                "list_path": list_path,
                "item_count": len(items),
                "object_sample_count": len(object_items),
                "sample_keys": sample_keys,
                "suggested_mapping": mapping,
                "warnings": warnings,
                "score": len(mapping) * 10 + min(len(object_items), 9),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["list_path"]))
    for item in candidates:
        item.pop("score", None)
    return {
        "list_candidates": candidates,
        "note": "Suggestions are heuristic; confirm against more than one challenge before creating a workflow.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest IPC challenge mappings from an offline JSON response."
    )
    parser.add_argument("source", help="JSON file path, or '-' for stdin")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()
    try:
        result = inspect(read_payload(args.source))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
