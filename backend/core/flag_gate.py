from __future__ import annotations

import re
from collections.abc import Iterable

# A flag must look like ``prefix{content}``: an alphanumeric prefix, exactly
# one outer brace pair, and non-empty content without nested braces.
DEFAULT_FLAG_PATTERN = r"^[A-Za-z0-9_-]+\{[^{}]+\}$"


def validate_flag(
    flag: str,
    pattern: str | None = None,
    rejected: Iterable[str] = (),
) -> str | None:
    """Local gate for member-reported flags.

    Returns a human-readable rejection reason, or ``None`` when the flag may
    proceed. A rejected flag never reaches the project record and never spends
    a platform submission attempt.
    """
    text = str(flag or "").strip()
    if not text:
        return "flag is empty"
    if text != str(flag):
        # Leading/trailing whitespace is tolerated silently, but the stored
        # and submitted value is the stripped one.
        flag = text
    compiled = pattern or DEFAULT_FLAG_PATTERN
    if not re.match(compiled, text):
        return (
            f"flag {text!r} does not match the required structure {compiled} "
            "(expected prefix{...} with no nested or empty braces)"
        )
    rejected_set = {str(item).strip() for item in rejected}
    if text in rejected_set:
        return (
            f"flag {text!r} was already REJECTED by the platform for this challenge; "
            "re-derive a different, complete flag instead of resubmitting it"
        )
    return None
