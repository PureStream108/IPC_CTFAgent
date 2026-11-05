from __future__ import annotations

"""One decision point for every command that would enter a task container.

Before this module the host-path rule was inlined at three call sites
(base_member bash, JobService.start and the operator task_sandbox_exec tool),
so a new rule had to be added three times and any new entry point skipped all
of them. Guard is the single place that answers "may this command run", returns
a rule id for the log, and keeps the "no docker exec" guarantee for a refused
command.

Two rule families live here:

* host_path - a command that reaches for the host or the IPC deployment tree.
  This already blocked before the refactor and still blocks unconditionally.
* destructive - a command that would break the target instead of testing it.
  The pattern table is NOT duplicated here: backend.pentest.guard already owns
  the destructive signatures (it is the detection layer of the pentest NEVER
  block) and this module reuses it so the two can never drift apart.

destructive starts in warn mode. The realistic false positive - a payload
string that merely contains rm -rf inside quotes - is real, and a wrong block
ends a solve. IPC_COMMAND_GUARD_MODE=enforce promotes it to a hard block once
the warning log has been reviewed, without a code change.

Install validation lives here too, because it is the same question - "is this
command allowed" - and the copy in base_member was the third definition of the
package-name regex.
"""

import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from backend.sandbox.command_policy import host_path_violation

GUARD_MODES = ("warn", "enforce")
DEFAULT_GUARD_MODE = "warn"

INSTALL_MANAGERS: dict[str, str] = {
    "apt": "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends",
    "pip": "python3 -m pip install --no-cache-dir",
    "gem": "gem install --no-document",
    "npm": "npm install -g",
    "go": "go install",
}
PACKAGE_NAME_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._+/@:=<>~^-]{0,127}$")
MAX_INSTALL_PACKAGES = 12


@dataclass(frozen=True)
class Decision:
    """The outcome of one policy check.

    allow carries the verdict, rule the stable id for log filtering and reason
    the sentence handed back to the model. enforced is False only for a rule
    running in warn mode, so a caller can log a warning without pretending the
    command was blocked.
    """

    allow: bool = True
    rule: str = ""
    reason: str = ""
    category: str = ""
    enforced: bool = True

    @property
    def blocked(self) -> bool:
        return not self.allow

    @property
    def warned(self) -> bool:
        return bool(self.rule) and self.allow and not self.enforced


def _mode_from_env() -> str:
    configured = os.environ.get("IPC_COMMAND_GUARD_MODE", "").strip().lower()
    return configured if configured in GUARD_MODES else DEFAULT_GUARD_MODE


class Guard:
    """Stateless policy checker; the mode is the only configuration."""

    def __init__(self, *, mode: str | None = None):
        self._explicit_mode = (mode or "").strip().lower() or None
        self.mode = self._explicit_mode or _mode_from_env()
        if self.mode not in GUARD_MODES:
            self.mode = DEFAULT_GUARD_MODE

    def current_mode(self) -> str:
        if self._explicit_mode:
            return self._explicit_mode
        return _mode_from_env()

    def check(self, command: str, *, kind: str = "bash", category: str = "") -> Decision:
        """Decide whether command may run for an action of kind.

        Rules run in order and the first hit wins, so the host-path rule (which
        is always enforced) is decided before the observational ones.
        """

        command = command if isinstance(command, str) else ""
        if not command.strip():
            return Decision(category=category)

        violation = host_path_violation(command)
        if violation is not None:
            return Decision(
                allow=False, rule="host_path", reason=violation,
                category="host_path", enforced=True,
            )

        destructive = self._destructive_hit(command)
        if destructive is not None:
            enforce = self.current_mode() == "enforce"
            return Decision(
                allow=not enforce,
                rule="destructive",
                reason=(
                    f"command looks destructive ({destructive}). Destructive actions "
                    "against a challenge or target are never allowed: test the bug, "
                    "do not break the service."
                ),
                category="destructive",
                enforced=enforce,
            )

        return Decision(category=category)

    @staticmethod
    def _destructive_hit(command: str) -> str | None:
        from backend.pentest.guard import scan

        for hit in scan(command, check_answers=False):
            if hit.get("kind") == "destructive":
                return str(hit.get("match") or hit.get("rule") or "destructive command")
        return None

    def check_install(self, manager: str, packages: Iterable[str]) -> Decision:
        """Validate one install action; the reason doubles as the rejection text."""

        manager = str(manager or "").strip().lower()
        collected = [str(item).strip() for item in (packages or ())]
        collected = [item for item in collected if item]
        if manager not in INSTALL_MANAGERS:
            choices = ", ".join(sorted(INSTALL_MANAGERS))
            return Decision(
                allow=False, rule="install_manager", category="install",
                reason=f"unsupported manager {manager!r}; use one of {choices}",
            )
        if not collected:
            return Decision(
                allow=False, rule="install_packages", category="install",
                reason="missing non-empty packages list",
            )
        if len(collected) > MAX_INSTALL_PACKAGES:
            return Decision(
                allow=False, rule="install_packages", category="install",
                reason=(
                    f"install is limited to {MAX_INSTALL_PACKAGES} packages per action"
                ),
            )
        invalid = [item for item in collected if not PACKAGE_NAME_RE.fullmatch(item)]
        if invalid:
            joined = ", ".join(invalid[:3])
            return Decision(
                allow=False, rule="install_packages", category="install",
                reason=f"invalid package name(s): {joined}",
            )
        return Decision(category="install")

    def install_command(self, manager: str, packages: Iterable[str]) -> str:
        """Build the shell command for an already validated install action."""

        prefix = INSTALL_MANAGERS[str(manager).strip().lower()]
        return prefix + " " + " ".join(shlex.quote(str(item)) for item in packages)


command_guard = Guard()


__all__ = [
    "DEFAULT_GUARD_MODE",
    "GUARD_MODES",
    "INSTALL_MANAGERS",
    "MAX_INSTALL_PACKAGES",
    "PACKAGE_NAME_RE",
    "Decision",
    "Guard",
    "command_guard",
]