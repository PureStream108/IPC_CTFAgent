from __future__ import annotations

import ast
import datetime as dt
import json
import math
import re
import warnings
from collections.abc import Iterator, Sequence
from typing import Any

import yaml


_MAX_INPUT_CHARS = 128_000
_MAX_CANDIDATES = 64
_MAX_DEPTH = 5
_MAX_DECODE_ATTEMPTS = 256
_MAX_TRUNCATED_ATTEMPTS = 32
_MAX_RELAXED_SOURCE_CHARS = 32_000
_MAX_RELAXED_CONTAINERS = 2_048
_MAX_VALUE_DEPTH = 32
_MAX_VALUE_NODES = 8_192
_MAX_VALUE_CHARS = 256_000
_FENCE_RE = re.compile(
    r"```(?:json|jsonc|javascript|js|python|yaml|yml)?\s*\r?\n?(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(
    r"<(?:json|action|tool_call|response)>\s*(.*?)\s*</(?:json|action|tool_call|response)>",
    re.IGNORECASE | re.DOTALL,
)
_PRIORITY_KEYS = (
    "next_action",
    "decision",
    "tool_call",
    "tool_calls",
    "function_call",
    "response",
    "result",
    "output",
    "message",
    "content",
    "data",
    "function",
    "arguments",
    "input",
)


def json_dict_candidates(value: Any) -> list[dict[str, Any]]:
    """Return bounded, best-effort object candidates from model-produced data.

    This accepts common LLM wire variations while deliberately refusing to
    invent unfinished string contents. Protocol-specific validation belongs to
    the caller; this function only recovers syntactically complete values.
    """

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    visited: set[int] = set()

    def add(candidate: dict[Any, Any]) -> None:
        if len(candidates) >= _MAX_CANDIDATES:
            return
        try:
            normalized = _json_safe_copy(candidate)
        except (TypeError, ValueError, RecursionError):
            return
        if not isinstance(normalized, dict):
            return
        try:
            fingerprint = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                default=repr,
            )
        except (TypeError, ValueError):
            fingerprint = repr(normalized)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        candidates.append(normalized)

    def visit(item: Any, depth: int) -> None:
        if depth > _MAX_DEPTH or len(candidates) >= _MAX_CANDIDATES:
            return
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in visited:
                return
            visited.add(identity)
        if isinstance(item, dict):
            add(item)
            lowered = {str(key).strip().lower(): child for key, child in item.items()}
            for key in _PRIORITY_KEYS:
                if key in lowered:
                    visit(lowered[key], depth + 1)
            for key, child in item.items():
                if str(key).strip().lower() in _PRIORITY_KEYS:
                    continue
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
                elif isinstance(child, str) and _looks_structured(child):
                    visit(child, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
            return
        if item is None:
            return
        text = item if isinstance(item, str) else str(item)
        text = text.strip()
        if not text:
            return
        text = text[:_MAX_INPUT_CHARS]
        for parsed in _parsed_values(text):
            if parsed is item:
                continue
            visit(parsed, depth + 1)

    visit(value, 0)
    return candidates


def _json_safe_copy(value: Any) -> Any:
    """Copy a parsed value into a bounded, acyclic JSON-compatible tree.

    PyYAML's safe loader prevents object construction but still permits aliases,
    including recursive aliases and compact expansion bombs. Copying with hard
    depth/node/text budgets keeps those structures away from agent dispatch,
    logging, and provider serialization.
    """

    active: set[int] = set()
    nodes = 0
    characters = 0

    def copy(item: Any, depth: int) -> Any:
        nonlocal nodes, characters
        nodes += 1
        if nodes > _MAX_VALUE_NODES:
            raise ValueError("parsed value exceeds node budget")
        if depth > _MAX_VALUE_DEPTH:
            raise ValueError("parsed value exceeds depth budget")
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            characters += len(item)
            if characters > _MAX_VALUE_CHARS:
                raise ValueError("parsed value exceeds text budget")
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else str(item)
        if isinstance(item, (dt.datetime, dt.date, dt.time)):
            return item.isoformat()
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
            characters += len(text)
            if characters > _MAX_VALUE_CHARS:
                raise ValueError("parsed value exceeds text budget")
            return text
        if isinstance(item, (dict, list, tuple, set, frozenset)):
            identity = id(item)
            if identity in active:
                raise ValueError("parsed value contains a recursive alias")
            active.add(identity)
            try:
                if isinstance(item, dict):
                    result: dict[str, Any] = {}
                    for key, child in item.items():
                        text_key = str(key)
                        characters += len(text_key)
                        if characters > _MAX_VALUE_CHARS:
                            raise ValueError("parsed value exceeds text budget")
                        result[text_key] = copy(child, depth + 1)
                    return result
                values = list(item)
                if isinstance(item, (set, frozenset)):
                    values.sort(key=repr)
                return [copy(child, depth + 1) for child in values]
            finally:
                active.remove(identity)
        text = str(item)
        characters += len(text)
        if characters > _MAX_VALUE_CHARS:
            raise ValueError("parsed value exceeds text budget")
        return text

    return copy(value, 0)


def extract_json_dict(
    value: Any,
    *,
    preferred_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Extract one object, preferring candidates that match protocol keys."""

    candidates = json_dict_candidates(value)
    if not candidates:
        raise ValueError("no JSON object found in model output")
    preferred = {_normalize_key(key) for key in preferred_keys}
    if preferred:
        for candidate in candidates:
            if preferred.intersection(_normalize_key(key) for key in candidate):
                return candidate
    return candidates[0]


def _parsed_values(text: str) -> Iterator[Any]:
    sources = [text]
    sources.extend(match.group(1).strip() for match in _FENCE_RE.finditer(text))
    sources.extend(match.group(1).strip() for match in _TAG_RE.finditer(text))
    stripped_fence = _strip_outer_fence(text)
    if stripped_fence != text:
        sources.append(stripped_fence)

    source_seen: set[str] = set()
    for source in sources:
        if not source or source in source_seen:
            continue
        source_seen.add(source)
        yield from _load_variants(source)

    decoder = json.JSONDecoder()
    attempts = 0
    for index, character in enumerate(text):
        if character not in "{[\"":
            continue
        attempts += 1
        if attempts > _MAX_DECODE_ATTEMPTS:
            break
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        yield parsed

    for source in _balanced_container_slices(text):
        yield from _load_variants(source)

    # Only container terminators are added. An unterminated quote returns None,
    # so a truncated command, URL, or argument can never be silently executed.
    for source in _truncated_container_slices(text):
        yield from _load_variants(source)


def _load_variants(source: str) -> Iterator[Any]:
    variants = [source]
    jsonc = _remove_trailing_commas(_strip_json_comments(source))
    if jsonc != source:
        variants.append(jsonc)
    seen: set[str] = set()
    for variant in variants:
        candidate = variant.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        loaders = [json.loads]
        if (
            len(candidate) <= _MAX_RELAXED_SOURCE_CHARS
            and candidate.count("{") + candidate.count("[") <= _MAX_RELAXED_CONTAINERS
        ):
            loaders.extend((_safe_literal_eval, yaml.safe_load))
        for loader in loaders:
            try:
                parsed = loader(candidate)
            except (
                json.JSONDecodeError,
                SyntaxError,
                ValueError,
                TypeError,
                RecursionError,
                yaml.YAMLError,
            ):
                continue
            if isinstance(parsed, (dict, list, str)):
                yield parsed
            break


def _safe_literal_eval(value: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.literal_eval(value)


def _looks_structured(value: str) -> bool:
    stripped = value.lstrip()
    return bool(
        stripped.startswith(("{", "[", "\"{", "\"[", "```", "<json", "<action", "<response"))
        or re.match(r"^[A-Za-z_][\w -]{0,40}\s*:", stripped)
    )


def _normalize_key(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().lower())


def _strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return text
    first_newline = stripped.find("\n")
    if first_newline < 0:
        return text
    return stripped[first_newline + 1 : -3].strip()


def _balanced_container_slices(text: str) -> Iterator[str]:
    stack: list[str] = []
    start: int | None = None
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if not stack:
            quote = None
            escaped = False
            if character in "{[":
                start = index
                stack.append("}" if character == "{" else "]")
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in "{[":
            stack.append("}" if character == "{" else "]")
            continue
        if character in "}]" and stack:
            if stack[-1] != character:
                stack.clear()
                start = None
                continue
            stack.pop()
            if not stack and start is not None:
                yield text[start : index + 1]
                start = None


def _truncated_container_slices(text: str) -> Iterator[str]:
    attempts = 0
    for start, character in enumerate(text):
        if character not in "{[":
            continue
        attempts += 1
        if attempts > _MAX_TRUNCATED_ATTEMPTS:
            break
        completed = _close_truncated_containers(text[start:].strip())
        if completed is not None:
            yield completed


def _close_truncated_containers(candidate: str) -> str | None:
    expected: list[str] = []
    quote: str | None = None
    escaped = False
    for character in candidate:
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in "{[":
            expected.append("}" if character == "{" else "]")
            continue
        if character in "}]":
            if not expected or expected[-1] != character:
                return None
            expected.pop()
            if not expected:
                return None
    if quote is not None or not expected:
        return None
    return candidate + "".join(reversed(expected))


def _strip_json_comments(text: str) -> str:
    output: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            output.append(character)
            index += 1
            continue
        next_character = text[index + 1] if index + 1 < len(text) else ""
        if character == "/" and next_character == "/":
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        if character == "/" and next_character == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                return text
            output.append(" ")
            index = end + 2
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _remove_trailing_commas(text: str) -> str:
    output: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            output.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)
