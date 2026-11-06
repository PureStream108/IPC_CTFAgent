"""Exceptions shared by native platform clients.

The orchestrator and the competition service classify failures generically:
a preflight refusal must not spend a platform attempt, and a rate limit is a
transient reason to retry later.  Platform packages inherit these classes so
shared code never imports a platform-specific exception type.
"""

from __future__ import annotations


class PlatformError(RuntimeError):
    """A platform request failed."""


class PlatformPreflightError(PlatformError):
    """A request was refused before it could consume a platform attempt."""


class PlatformRateLimitError(PlatformError):
    """The platform or the shared local quota refused further submissions."""


__all__ = [
    "PlatformError",
    "PlatformPreflightError",
    "PlatformRateLimitError",
]
