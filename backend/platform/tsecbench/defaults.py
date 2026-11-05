"""TSecBench-specific defaults and quirks, in one place.

Everything here was learned from real runs against the hosted evaluation
platform. It lives next to the adapter so the hosted bootstrap, the autopilot
and the tests all read the same values instead of re-deriving them.

No credentials, account ids, run ids or agent tags belong in this module: the
platform forbids baking secrets into an image, and the tests enforce it.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

GATEWAY_SUFFIX = ".tsecbench.gw"
GATEWAY_SCHEME = "http"

KNOWN_BASE_URLS = {
    "anthropic": "https://api.deepseek.com/anthropic",
    "openai": "https://api.deepseek.com",
    "deepseek": "https://api.deepseek.com",
}

SUPPORTED_API_FORMATS = ("openai", "anthropic", "deepseek", "pi", "mock")

DEFAULT_MODEL = "deepseek-flash"

PROVIDER_TIMEOUTS = {"read": 120, "turn": 300, "progress": 120}

HOSTED_DEFAULTS = {
    # 0 disables the local guard: the platform task window is authoritative.
    "duration_minutes": 0,
    "remote_concurrency": 3,
    "per_challenge_minutes": 45,
    "poll_seconds": 20,
    "writeups": False,
    "allow_hints": True,
    "hint_at_attempt": 2,
    "max_compactions": 1,
    "park_digest": True,
}

ENV_KEYS = (
    "IPC_LLM_API_FORMAT",
    "IPC_LLM_BASE_URL",
    "IPC_LLM_MODEL",
    "IPC_LLM_REASONING_EFFORT",
    "IPC_LLM_API_KEY",
    "IPC_DIAMOND_API_FORMAT",
    "IPC_DIAMOND_BASE_URL",
    "IPC_DIAMOND_MODEL",
    "IPC_DIAMOND_REASONING_EFFORT",
    "IPC_DIAMOND_API_KEY",
    "IPC_OPS_MODEL",
    "IPC_OPS_API_KEY",
    "IPC_HOSTED_DURATION_MINUTES",
    "IPC_HOSTED_REMOTE_CONCURRENCY",
    "IPC_HOSTED_PER_CHALLENGE_MINUTES",
    "IPC_WP_JOBS",
    "IPC_PARK_DIGEST",
    "IPC_HINT_AT_ATTEMPT",
    "IPC_MAX_COMPACTIONS_PER_ATTEMPT",
)

PLATFORM_NOTES = (
    (
        "An instance address is recycled: the same IP:port served eleven different "
        "challenges in one run, so a cached address becomes another challenge's "
        "container the moment the instance closes."
    ),
    (
        "The instance lifecycle is start/stop per challenge with a five hour "
        "deadline anchored at the first assignment; parking closes the box."
    ),
    (
        "A hint costs a fraction of that challenge's score and the platform records "
        "the view, so buy one only for a target that already failed."
    ),
    (
        "Multi-flag challenges report correct_flag_count/total_flag_count; the "
        "completion flag is only trustworthy when both counts are present."
    ),
    (
        "The task window belongs to the platform, not to a local constant: the "
        "run must follow the declared end and drain there, not stop early."
    ),
)


def via_gateway(url: str, *, suffix: str = GATEWAY_SUFFIX,
                scheme: str = GATEWAY_SCHEME) -> str:
    """Rewrite a public base URL onto the platform gateway."""

    if not url:
        return url
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host or host.endswith(suffix):
        return url
    netloc = host + suffix + (":" + str(parts.port) if parts.port else "")
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def default_model(api_format: str, base_url: str) -> str:
    """Pick the model the platform gateway expects for this host."""

    del api_format
    return DEFAULT_MODEL


def origin(url: str) -> str:
    """Reduce a URL to scheme://host:port, which is what a workflow stores."""

    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.hostname:
        return (url or "").strip()
    netloc = parts.hostname + (":" + str(parts.port) if parts.port else "")
    return urlunsplit((parts.scheme, netloc, "", "", ""))


__all__ = [
    "DEFAULT_MODEL",
    "ENV_KEYS",
    "GATEWAY_SCHEME",
    "GATEWAY_SUFFIX",
    "HOSTED_DEFAULTS",
    "KNOWN_BASE_URLS",
    "PLATFORM_NOTES",
    "PROVIDER_TIMEOUTS",
    "SUPPORTED_API_FORMATS",
    "default_model",
    "origin",
    "via_gateway",
]