from __future__ import annotations

import re
from collections.abc import Iterable

DIFFICULTY_LEVELS: tuple[str, ...] = ("low", "medium", "high", "ex")
DIFFICULTY_RANK: dict[str, int] = {level: idx for idx, level in enumerate(DIFFICULTY_LEVELS)}

_ALIASES = {
    "trivial": "low",
    "easy": "low",
    "normal": "medium",
    "moderate": "medium",
    "med": "medium",
    "hard": "high",
    "expert": "ex",
    "extreme": "ex",
    "extra": "ex",
}

_EXPLOIT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sqli", ("sql", "sqli", "union select", "blind injection")),
    ("xss", ("xss", "script", "dom clobber", "csp")),
    ("ssti", ("ssti", "template injection", "jinja", "freemarker", "twig")),
    ("ssrf", ("ssrf", "gopher", "metadata", "169.254.169.254")),
    ("rce", ("rce", "command injection", "shell", "deserialization", "pickle", "yaml.load")),
    ("lfi", ("lfi", "path traversal", "../", "file read", "local file")),
    ("auth", ("jwt", "session", "cookie", "csrf", "auth bypass", "idor", "oauth")),
    ("crypto", ("rsa", "lattice", "modulus", "oracle", "padding oracle", "ecc", "aes")),
    ("pwn", ("rop", "overflow", "format string", "heap", "got", "libc", "ret2")),
    ("reverse", ("decompile", "angr", "ghidra", "ida", "patch", "anti-debug")),
    ("stego", ("stego", "exif", "lsb", "zsteg", "binwalk", "foremost")),
)

_SURFACE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("web", r"https?://|/admin|/login|endpoint|route|api\b|upload|cookie|jwt|session"),
    ("service", r"\bport\s*\d+|\b\d{2,5}/tcp\b|nc\s+|socket|grpc|redis|mysql|ssh"),
    ("binary", r"\.elf\b|binary|libc|heap|stack|rop|overflow|format string"),
    ("source", r"source code|\.py\b|\.php\b|\.js\b|\.java\b|\.go\b|repository|dockerfile"),
    ("crypto", r"rsa|aes|ecc|cipher|oracle|modulus|lattice"),
    ("file", r"attachment|distfile|pcap|image|png|jpg|zip|tar|pdf|firmware"),
)


def normalize_difficulty(value: str | None) -> str:
    text = (value or "low").strip().lower()
    text = _ALIASES.get(text, text)
    return text if text in DIFFICULTY_RANK else "low"


def max_difficulty(*values: str | None) -> str:
    best = "low"
    for value in values:
        level = normalize_difficulty(value)
        if DIFFICULTY_RANK[level] > DIFFICULTY_RANK[best]:
            best = level
    return best


def extra_members_for_difficulty(value: str | None) -> int:
    # Keep reinforcements conservative: high means a second pair of eyes, not
    # instantly spending every configured member on a possibly stale branch.
    return {"low": 0, "medium": 1, "high": 1, "ex": 2}[normalize_difficulty(value)]


def detect_exploit_classes(texts: Iterable[str]) -> set[str]:
    haystack = "\n".join(text for text in texts if text).lower()
    classes: set[str] = set()
    for name, needles in _EXPLOIT_PATTERNS:
        if any(needle in haystack for needle in needles):
            classes.add(name)
    return classes


def detect_attack_surfaces(texts: Iterable[str]) -> set[str]:
    haystack = "\n".join(text for text in texts if text).lower()
    surfaces: set[str] = set()
    for name, pattern in _SURFACE_PATTERNS:
        if re.search(pattern, haystack):
            surfaces.add(name)
    return surfaces
