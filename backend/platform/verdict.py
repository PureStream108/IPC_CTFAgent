"""Platform verdicts are explicit application evidence, never HTTP success."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

VerdictStatus = Literal["correct", "wrong", "pending", "unknown", "rate_limited", "auth_required", "rejected"]
COOLDOWN_SECONDS = (30, 60, 90, 120, 150, 300)


def cooldown_seconds(wrong_count: int) -> int:
    if wrong_count < 0:
        raise ValueError("wrong count must not be negative")
    return 0 if wrong_count == 0 else COOLDOWN_SECONDS[min(wrong_count, 6) - 1]


def json_value(payload: Any, path: str) -> Any:
    value = payload
    for part in path.split("."):
        if isinstance(value, dict):
            value = value[part]
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def matches(value: Any, values: list[Any]) -> bool:
    # Python treats True == 1; that must not confuse platform verdict codes.
    return any(type(value) is type(expected) and value == expected for expected in values)


@dataclass(frozen=True)
class Verdict:
    status: VerdictStatus
    status_code: int
    submission_id: str | None = None
    retry_after: int | None = None

    @property
    def correct(self) -> bool:
        return self.status == "correct"


def interpret_response(response: Any, spec: Any) -> Verdict:
    code = int(getattr(response, "status_code", 0))
    if code in (401, 403):
        return Verdict("auth_required", code)
    if code == 429:
        raw = str(getattr(response, "headers", {}).get("Retry-After", ""))
        try:
            delay = max(0, int(raw))
        except ValueError:
            try:
                delay = max(0, int((parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                delay = 60
        return Verdict("rate_limited", code, retry_after=delay)
    if code not in spec.success_statuses:
        return Verdict("unknown" if code >= 500 or not code else "rejected", code)
    try:
        payload = response.json()
        value = json_value(payload, spec.success_path) if spec.success_path else None
        submission_id = str(json_value(payload, spec.submission_id_path)) if spec.submission_id_path else None
    except (KeyError, IndexError, TypeError, ValueError):
        return Verdict("unknown", code)
    if spec.success_path:
        for status, values in (("correct", spec.success_values), ("wrong", spec.wrong_values), ("pending", spec.pending_values)):
            if matches(value, values):
                return Verdict(status, code, submission_id=submission_id)
    return Verdict("unknown", code, submission_id=submission_id)
