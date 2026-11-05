from __future__ import annotations

"""S3: every command that reaches a container is judged in one place.

Each rule needs a positive and a negative case, and a refusal has to be
provable without touching Docker: the tests assert that the sandbox was never
called, because "blocked" only means something if nothing ran.
"""

import pytest

from backend.agent.job_service import JobService
from backend.sandbox.command_policy import host_path_violation
from backend.sandbox.guard import PACKAGE_NAME_RE, Guard, command_guard


@pytest.mark.parametrize(
    "command",
    [
        "cat D:/Desktop/Codex/IPC_CTFAgent/.qa-artifacts/run/WriteUp.pdf",
        "type C:\\Windows\\win.ini",
        "python solve.py E:/ctf/chall.zip",
        "/d/Language/Python314/python solve.py",
        "chroot /host /bin/bash -lc id",
        "cat data/artifacts/writeups/proj_1.md",
    ],
)
def test_host_path_rule_blocks_and_keeps_the_original_reason(command):
    """The message must stay identical: tests and the prompt quote it."""

    decision = command_guard.check(command)

    assert decision.blocked is True
    assert decision.rule == "host_path"
    assert decision.enforced is True
    assert decision.reason == host_path_violation(command)
    assert "command blocked" not in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "ls -la /workspace/shared",
        "python3 -c 'print(1)'",
        "curl -s http://10.0.3.15:8080/admin",
        # 081 PHP Deserialization: s:%d:"%s" used to be read as the drive d:.
        'python3 -c \'s="O:8:\\"stdClass\\":1:{s:8:\\"username\\";s:%d:\\"%s\\";}"\'',
        'php -r \'echo unserialize("O:8:\\"stdClass\\":1:{s:8:\\"u\\";s:4:\\"d:\\\\x\\";}");\'',
        'printf \'%d:\\"%s\\"\' 5 hello',
        'echo \'s:4:"d:\\x"\' | base64',
    ],
)
def test_container_commands_are_clean(command):
    decision = command_guard.check(command)

    assert decision.blocked is False
    assert decision.rule == ""
    assert decision.warned is False


def test_a_serialized_prefix_does_not_excuse_a_real_host_path():
    """The payload exception covers the data token only, not the rest of the line."""

    command = 'echo \'s:4:"d:\\x"\' ; type C:\\Windows\\win.ini'
    assert host_path_violation(command) is not None
    assert host_path_violation('echo \'s:4:"d:\\x"\' ; cat data/artifacts/writeups/x.md')


def test_destructive_is_a_warning_until_the_mode_is_promoted():
    """A quoted payload string must not end a solve in the default mode."""

    command = "curl -s -d 'cmd=shutdown -h now' http://target.test/api"

    warning = Guard(mode="warn").check(command)
    assert warning.blocked is False
    assert warning.rule == "destructive"
    assert warning.warned is True

    enforced = Guard(mode="enforce").check(command)
    assert enforced.blocked is True
    assert enforced.rule == "destructive"
    assert enforced.enforced is True
    assert "destructive" in enforced.reason


def test_the_destructive_table_is_not_duplicated_in_the_sandbox():
    """One pattern source: backend.pentest.guard owns the signatures."""

    import backend.sandbox.guard as sandbox_guard

    with open(sandbox_guard.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "mkfs" not in source
    assert "fork bomb" not in source


def test_an_empty_or_missing_command_is_allowed_through():
    assert command_guard.check("").blocked is False
    assert command_guard.check(None).blocked is False


@pytest.mark.parametrize(
    ("manager", "packages", "rule"),
    [
        ("curl | sh", ["x"], "install_manager"),
        ("pip", [], "install_packages"),
        ("pip", ["ok"] * 13, "install_packages"),
        ("pip", ["bad name"], "install_packages"),
    ],
)
def test_install_rule_rejects(manager, packages, rule):
    decision = command_guard.check_install(manager, packages)

    assert decision.blocked is True
    assert decision.rule == rule
    assert decision.reason


@pytest.mark.parametrize(
    ("manager", "packages"),
    [
        ("pip", ["pymupdf"]),
        ("apt", ["jq", "curl"]),
        ("npm", ["@scope/pkg"]),
    ],
)
def test_install_rule_accepts(manager, packages):
    decision = command_guard.check_install(manager, packages)
    assert decision.blocked is False
    assert command_guard.install_command(manager, packages).endswith(" ".join(packages))


def test_install_command_quotes_shell_metacharacters():
    """The name regex allows > and =, so an unquoted install would redirect."""

    assert PACKAGE_NAME_RE.fullmatch("pkg>shadow") is not None
    command = command_guard.install_command("pip", ["pkg>shadow"])
    assert "'pkg>shadow'" in command


class ExplodingStore:
    def start(self, *args, **kwargs):  # pragma: no cover - the assertion is the test
        raise AssertionError("the policy check must run before the job is recorded")


class ExplodingSandbox:
    def exec_detached(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("a blocked command reached the container")


def test_a_refused_job_never_reaches_the_container():
    service = JobService(ExplodingStore(), ExplodingSandbox(), "session-1")

    result = service.start("cat /host/etc/shadow")

    assert result["ok"] is False
    assert "blocked" in result["error"]


def test_a_refused_job_error_carries_the_rule_reason():
    service = JobService(ExplodingStore(), ExplodingSandbox(), "session-1")

    result = service.start("/d/Language/Python314/python solve.py")

    assert "host" in result["error"].lower()