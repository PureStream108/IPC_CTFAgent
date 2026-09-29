"""Reject member commands that reach outside the task container.
"""

from __future__ import annotations

import re

_WINDOWS_DRIVE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_MSYS_DRIVE = re.compile(r"(?<![A-Za-z0-9_./])/[a-zA-Z]/(?:Users|Windows|Program|Language|Desktop)\b")
_HOST_MOUNT = re.compile(r"(?<![A-Za-z0-9_./])/host(?:/|\b)")

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


def host_path_violation(command: str) -> str | None:
    """Return a human-readable reason when ``command`` references the host.

    Returns ``None`` for commands that stay inside the container.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    for pattern, description in _PATTERNS:
        match = pattern.search(command)
        if match is None:
            continue
        return (
            f"command references {description} ({match.group(0)!r}). Every file for this "
            "challenge is inside the task container: work under /workspace/shared, "
            "/workspace/attachments or /tmp, and use the container's own python3. "
            "Reading a writeup or solution file instead of solving the challenge is cheating."
        )
    return None
