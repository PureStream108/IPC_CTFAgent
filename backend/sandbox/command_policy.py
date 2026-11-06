from __future__ import annotations

import re

# A Windows drive is only a host path at the start of a word. The old rule
# excluded only letters/digits/underscore, so the "d:" in a PHP serialization
# format string (s:%d:"%s") matched and blocked a legitimate payload.
_DRIVE_START = r"(?:^|(?<=[\s'\"=(\[{,;|&]))"
_WINDOWS_DRIVE = re.compile(rf"{_DRIVE_START}[A-Za-z]:[\\/](?![%])")
_MSYS_DRIVE = re.compile(r"(?<![A-Za-z0-9_./])/[a-zA-Z]/(?:Users|Windows|Program|Language|Desktop)\b")
_HOST_MOUNT = re.compile(r"(?<![A-Za-z0-9_./])/host(?:/|\b)")

# PHP/pickle-style serialization uses s:<n>:"<value>" and the printf form
# s:%d:"%s". A drive-looking token directly inside that prefix is data, not a
# path, so the drive rule is skipped for that match (the /host and deployment
# rules still apply).
_SERIALIZED_PREFIX = re.compile(r"s:(?:\d+|%d):\\?['\"]?\\?$")

_DEPLOYMENT_PATHS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"\.qa-artifacts"
    r"|data/artifacts"
    r"|artifacts/(?:writeups|exports|logs)"
    r"|IPC_CTFAgent"
    r")(?![A-Za-z0-9_-])"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_WINDOWS_DRIVE, "a Windows host drive path"),
    (_MSYS_DRIVE, "a Windows host drive path"),
    (_HOST_MOUNT, "the privileged /host mount"),
    (_DEPLOYMENT_PATHS, "an IPC deployment directory (writeups, exports or QA artifacts)"),
)


def _serialized_payload(command: str, start: int) -> bool:
    """True when the drive-like token at start sits inside a serialized value."""

    return bool(_SERIALIZED_PREFIX.search(command[max(0, start - 48):start]))


def host_path_violation(command: str) -> str | None:
    """Return a human-readable reason when command references the host.

    Returns None for commands that stay inside the container.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    for pattern, description in _PATTERNS:
        for match in pattern.finditer(command):
            if description == "a Windows host drive path" and _serialized_payload(
                command, match.start()
            ):
                continue
            return (
                f"command references {description} ({match.group(0)!r}). Every file for this "
                "challenge is inside the task container: work under /workspace/shared, "
                "/workspace/attachments or /tmp, and use the container's own python3. "
                "Reading a writeup or solution file instead of solving the challenge is cheating."
            )
    return None
