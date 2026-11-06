"""The TSecBench defaults module must stay secret-free and correct."""

from __future__ import annotations

import re
from pathlib import Path

from backend.platform.tsecbench import defaults

PACKAGE = Path(defaults.__file__).parent


def test_gateway_rewrite_matches_the_platform() -> None:
    assert defaults.via_gateway("https://api.deepseek.com") == (
        "http://api.deepseek.com.tsecbench.gw"
    )
    assert defaults.via_gateway("https://api.deepseek.com/anthropic") == (
        "http://api.deepseek.com.tsecbench.gw/anthropic"
    )
    already = "http://api.deepseek.com.tsecbench.gw/v1"
    assert defaults.via_gateway(already) == already


def test_origin_is_what_a_workflow_stores() -> None:
    assert defaults.origin("http://api.deepseek.com.tsecbench.gw/v1/chat") == (
        "http://api.deepseek.com.tsecbench.gw"
    )
    assert defaults.origin("https://host.example:8443/x") == "https://host.example:8443"


def test_hosted_defaults_cover_the_knobs_the_image_reads() -> None:
    keys = set(defaults.ENV_KEYS)
    assert "IPC_LLM_API_KEY" in keys and "IPC_DIAMOND_API_KEY" in keys
    assert defaults.HOSTED_DEFAULTS["duration_minutes"] == 0
    assert defaults.HOSTED_DEFAULTS["remote_concurrency"] == 3
    assert defaults.HOSTED_DEFAULTS["per_challenge_minutes"] == 45
    assert defaults.HOSTED_DEFAULTS["writeups"] is False
    assert defaults.PROVIDER_TIMEOUTS["turn"] >= 120


def test_package_contains_no_credentials() -> None:
    """A hosted image may not bake secrets in, so the source must be clean."""

    patterns = [
        re.compile(r"sk-[A-Za-z0-9]{12,}"),
        re.compile(r"AKID[A-Za-z0-9]{10,}"),
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\."),
        re.compile(r"BENCHMARK_TOKEN\s*=\s*[A-Za-z0-9-]{8,}"),
    ]
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(path.name + ": " + pattern.pattern)
    assert not offenders, offenders


def test_defaults_are_the_only_place_that_knows_the_gateway() -> None:
    """The suffix must not be re-declared in another module of the package."""

    declarations = [
        path.name
        for path in PACKAGE.rglob("*.py")
        if ".tsecbench.gw" in path.read_text(encoding="utf-8")
        and path.name not in {"defaults.py"}
    ]
    assert declarations == []