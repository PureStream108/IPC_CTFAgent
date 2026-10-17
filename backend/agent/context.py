from __future__ import annotations

import json
from typing import Any

_SUMMARY_PREFACE = (
    "[compacted context summary] Earlier turns of this session were "
    "compacted. Treat the following as established evidence and do not redo "
    "the work it describes:\n"
)

_CJK_CHARS_PER_TOKEN = 2.2
_ASCII_CHARS_PER_TOKEN = 3.8
_PER_MESSAGE_TOKENS = 4
_MIN_CALIBRATION = 0.5
_MAX_CALIBRATION = 2.0
_CALIBRATION_WEIGHT = 0.3


_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude-opus-5", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-fable-5", 1_000_000),
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-3", 200_000),
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("o4", 200_000),
    ("o3", 200_000),
    ("deepseek", 1_000_000),
    ("kimi-k2", 256_000),
    ("kimi-for-coding", 256_000),
    ("qwen3", 128_000),
    ("glm-4", 128_000),
)
_WINDOW_RESERVE_RATIO = 0.9


def infer_context_limit(
    model_snapshot: Any, *, max_output_tokens: int = 0
) -> int:
    """Infer a usable prompt-token budget from a model name.

    Returns 0 when the model is unknown, which leaves token-based compaction
    off rather than imposing a guessed ceiling. That is the safe direction:
    without a limit the byte ceiling still bounds the context, whereas a limit
    guessed too low silently discards findings mid-investigation.
    """
    name = _model_name(model_snapshot)
    if not name:
        return 0
    window = 0
    matched = ""
    for marker, published in _CONTEXT_WINDOWS:
        if marker in name and len(marker) > len(matched):
            window, matched = published, marker
    if not window:
        return 0
    usable = int(window * _WINDOW_RESERVE_RATIO) - max(0, max_output_tokens)
    return usable if usable > 0 else int(window * _WINDOW_RESERVE_RATIO)


def _model_name(model_snapshot: Any) -> str:
    if isinstance(model_snapshot, str):
        candidate = model_snapshot
    elif isinstance(model_snapshot, dict):
        candidate = str(model_snapshot.get("model") or "")
    else:
        candidate = str(getattr(model_snapshot, "model", "") or "")
    return candidate.strip().lower().rsplit("/", 1)[-1]


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
