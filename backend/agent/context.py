"""Context projection: rebuild what the provider sees from durable events.

The reconstruction rule is deliberately simple and total: the newest
compaction's summary leads, followed by every message the compaction retained
and every event recorded after it. Because ``agent_events`` is append-only, a
projection decision is never destructive - a bad compaction costs one turn of
context quality, not the evidence chain.

Budgeting works in tokens rather than bytes. A byte threshold is wrong by a
factor of roughly two between CJK and ASCII text, which in practice means
compacting far too late (context overflow) or far too early (throwing away
usable evidence). The estimate is calibrated against the real
``usage.prompt_tokens`` the provider reports, so it converges on the tokenizer
actually in use instead of assuming one.
"""
from __future__ import annotations

import json
from typing import Any

_SUMMARY_PREFACE = (
    "[compacted context summary] Earlier turns of this session were "
    "compacted. Treat the following as established evidence and do not redo "
    "the work it describes:\n"
)

# Characters per token, measured separately because the ratio differs sharply:
# CJK text is close to one token per character or two, while English prose is
# closer to four characters per token.
_CJK_CHARS_PER_TOKEN = 2.2
_ASCII_CHARS_PER_TOKEN = 3.8
# Every message carries role/delimiter overhead regardless of its content.
_PER_MESSAGE_TOKENS = 4
# Keep calibration inside a sane band: one anomalous response (a cache hit, a
# provider that counts differently) must not distort the budget for the rest
# of the session.
_MIN_CALIBRATION = 0.5
_MAX_CALIBRATION = 2.0
# Exponential smoothing weight for a new observation.
_CALIBRATION_WEIGHT = 0.3


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0x3000 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for character in text if _is_cjk(character))
    other = len(text) - cjk
    return int(cjk / _CJK_CHARS_PER_TOKEN + other / _ASCII_CHARS_PER_TOKEN) + 1


def estimate_tokens(messages: list[dict[str, Any]], *, calibration: float = 1.0) -> int:
    """Estimate the prompt tokens of a projection.

    ``calibration`` scales the heuristic by what the provider actually charged
    for a comparable prompt; see :func:`calibrate`.
    """
    total = 0
    for message in messages:
        total += _PER_MESSAGE_TOKENS
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                total += estimate_text_tokens(content)
            elif content is not None:
                total += estimate_text_tokens(
                    json.dumps(content, ensure_ascii=False)
                )
            for key in ("tool_calls", "tool_call_id", "name"):
                extra = message.get(key)
                if extra:
                    total += estimate_text_tokens(
                        extra if isinstance(extra, str)
                        else json.dumps(extra, ensure_ascii=False)
                    )
        else:
            total += estimate_text_tokens(json.dumps(message, ensure_ascii=False))
    return max(1, int(total * _clamp_calibration(calibration)))


def _clamp_calibration(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if number <= 0:
        return 1.0
    return max(_MIN_CALIBRATION, min(_MAX_CALIBRATION, number))


def calibrate(previous: float, estimated: int, actual: int | None) -> float:
    """Fold a real ``prompt_tokens`` observation into the calibration ratio."""
    if not actual or actual <= 0 or estimated <= 0:
        return _clamp_calibration(previous)
    observed = actual / estimated
    blended = (
        _clamp_calibration(previous) * (1 - _CALIBRATION_WEIGHT)
        + observed * _CALIBRATION_WEIGHT
    )
    return _clamp_calibration(blended)


def should_compact(
    messages: list[dict[str, Any]],
    *,
    limit: int,
    trigger_ratio: float = 0.75,
    calibration: float = 1.0,
) -> bool:
    """Decide whether the projection is close enough to the limit to compact."""
    if limit <= 0:
        return False
    estimate = estimate_tokens(messages, calibration=calibration)
    return estimate > limit * trigger_ratio


def render_summary(summary: Any) -> str:
    """Render a summary payload as text, accepting legacy plain strings."""
    if isinstance(summary, str):
        return summary
    if not summary:
        return ""
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def summary_message(summary: Any) -> dict[str, Any] | None:
    """Build the user message that injects a summary, or None if it is empty."""
    text = render_summary(summary)
    if not text.strip():
        return None
    return {"role": "user", "content": _SUMMARY_PREFACE + text}


def compacted_history(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a compaction payload into provider messages.

    A compaction keeps a tail of the transcript; the summary stands in for
    everything dropped, so it has to lead. Omitting it is what silently loses
    every finding made before the compaction.
    """
    messages = list(payload.get("messages") or [])
    leading = summary_message(payload.get("summary"))
    return [leading, *messages] if leading else messages
