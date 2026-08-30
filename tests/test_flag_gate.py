from __future__ import annotations

from backend.core.flag_gate import DEFAULT_FLAG_PATTERN, validate_flag


def test_valid_flag_passes():
    assert validate_flag("moectf{hello_world}") is None
    assert validate_flag("flag{mock_solved}") is None


def test_empty_flag_rejected():
    assert validate_flag("") is not None
    assert validate_flag("   ") is not None


def test_bare_string_rejected():
    # Ultra Potato case: a random string without braces is not a flag.
    assert validate_flag("Fnrr68qUabc123") is not None


def test_nested_braces_rejected():
    # Polyglot case: a ring-splice window containing '}{' is not a flag.
    assert validate_flag("moectf{abc}{def}") is not None


def test_empty_content_rejected():
    assert validate_flag("moectf{}") is not None


def test_surrounding_whitespace_tolerated():
    assert validate_flag("  moectf{ok}  ") is None


def test_custom_pattern():
    assert validate_flag("moectf{x}", pattern=r"^flag\{.*\}$") is not None
    assert validate_flag("flag{x}", pattern=r"^flag\{.*\}$") is None


def test_rejected_blacklist_blocks_resubmission():
    reason = validate_flag("moectf{keep_trying}", rejected=["moectf{keep_trying}"])
    assert reason is not None
    assert "REJECTED" in reason


def test_default_pattern_allows_common_prefixes():
    assert validate_flag("moectf{a-b_C123}") is None
    assert validate_flag("moectf{space inside}", rejected=[]) is None  # spaces allowed in content
