"""Semantic compaction: a structured summary, not a truncated transcript.

Dropping the oldest messages loses exactly what a long investigation needs to
keep: which facts were verified, which approaches already failed, and where the
interesting bytes live. That is why the summary has an explicit schema with a
``do_not_retry`` marker on failed paths - the point is to stop the model from
re-running work it has already ruled out.

Compaction is an optimisation, never a precondition for running. If the
summarizer fails, the caller falls back to :func:`deterministic_summary`, which
needs no model at all.
"""
from __future__ import annotations

import json
from typing import Any

SUMMARY_SCHEMA_HINT = {
    "verified_facts": [{"claim": "...", "evidence": "file:offset | command | artifact_id"}],
    "failed_paths": [{"approach": "...", "why_failed": "...", "do_not_retry": True}],
    "open_questions": ["..."],
    "artifacts": [{"path": "...", "role": "...", "sha256": "..."}],
    "key_offsets": [{"file": "...", "offset": "...", "meaning": "..."}],
    "next_hypotheses": ["..."],
}

_SUMMARY_KEYS = tuple(SUMMARY_SCHEMA_HINT)

_PROMPT = (
    "You are compacting the earlier part of your own working session so it "
    "fits the model context. Preserve what future turns need and nothing else. "
    "Return only a JSON object with exactly these keys:\n"
    + json.dumps(SUMMARY_SCHEMA_HINT, ensure_ascii=False, indent=2)
    + "\n\nRules:\n"
    "- verified_facts: only claims with concrete evidence you actually saw.\n"
    "- failed_paths: approaches already ruled out. Set do_not_retry to true so "
    "a later turn does not repeat them.\n"
    "- key_offsets: file positions, addresses and structure offsets worth "
    "remembering exactly.\n"
    "- Never invent a finding that is not in the transcript.\n"
    "- Do not include credentials or API keys."
)


def empty_summary() -> dict[str, Any]:
    return {key: [] for key in _SUMMARY_KEYS}


def normalize_summary(value: Any) -> dict[str, Any]:
    """Coerce a model response into the summary schema, dropping junk."""
    summary = empty_summary()
    if not isinstance(value, dict):
        return summary
    for key in _SUMMARY_KEYS:
        item = value.get(key)
        if isinstance(item, list):
            summary[key] = item
        elif item:
            summary[key] = [item]
    return summary


def is_empty(summary: dict[str, Any]) -> bool:
    return not any(summary.get(key) for key in _SUMMARY_KEYS)


def deterministic_summary(
    messages: list[dict[str, Any]], *, keep_recent: int = 8
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compact without a model: keep the opening task and the recent tail.

    This is the availability floor. It records the tool calls that were made so
    the model still knows what ground it covered, even though it cannot
    summarize the findings.
    """
    if not messages:
        return empty_summary(), []
    head = messages[:1]
    tail = _balanced_tail(messages[1:], keep_recent)
    dropped = messages[1: len(messages) - len(tail)]
    signatures: list[str] = []
    for message in dropped:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                function = call.get("function") or {}
                name = function.get("name") if isinstance(function, dict) else None
                if name:
                    signatures.append(str(name))
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    signatures.append(str(block.get("name", "")))
    summary = empty_summary()
    if dropped:
        summary["open_questions"] = [
            "The earlier transcript was compacted without a semantic summary; "
            "re-derive any detail you need instead of assuming it."
        ]
    if signatures:
        seen: list[str] = []
        for name in signatures:
            if name and name not in seen:
                seen.append(name)
        summary["verified_facts"] = [
            {
                "claim": f"Tools already invoked earlier in this session: {', '.join(seen)}",
                "evidence": "compacted transcript",
            }
        ]
    return summary, [*head, *tail]


def _balanced_tail(
    messages: list[dict[str, Any]], keep_recent: int
) -> list[dict[str, Any]]:
    """Take a tail that does not start with an orphaned tool result.

    A transcript beginning with a tool result whose assistant tool call was
    dropped is rejected by every provider, so the cut has to move.
    """
    if keep_recent <= 0 or not messages:
        return []
    tail = messages[-keep_recent:] if keep_recent < len(messages) else list(messages)
    while tail and _is_orphaned_tool_result(tail[0], tail):
        tail = tail[1:]
    return tail


def _is_orphaned_tool_result(
    message: dict[str, Any], tail: list[dict[str, Any]]
) -> bool:
    if not isinstance(message, dict):
        return False
    call_id = message.get("tool_call_id")
    if message.get("role") == "tool" and call_id:
        return not any(
            isinstance(other, dict)
            and any(
                isinstance(call, dict) and call.get("id") == call_id
                for call in other.get("tool_calls") or []
            )
            for other in tail
        )
    content = message.get("content")
    if message.get("role") == "user" and isinstance(content, list):
        # Anthropic carries tool results inside a user message.
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def summarize(
    messages: list[dict[str, Any]],
    *,
    request,
    keep_recent: int = 8,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Produce a structured summary plus the retained tail.

    ``request`` takes the compaction prompt and returns the model's raw text.
    Any failure or unusable response degrades to
    :func:`deterministic_summary` rather than raising: losing summary quality
    costs context, while raising would cost the whole task.
    """
    fallback_summary, retained = deterministic_summary(
        messages, keep_recent=keep_recent
    )
    dropped = messages[1: len(messages) - len(retained) + 1]
    if not dropped:
        return fallback_summary, retained
    try:
        raw = request(_PROMPT, dropped)
        summary = normalize_summary(_parse_json(raw))
    except Exception:
        return fallback_summary, retained
    if is_empty(summary):
        return fallback_summary, retained
    return summary, retained


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # A model often wraps the object in prose or a code fence.
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
