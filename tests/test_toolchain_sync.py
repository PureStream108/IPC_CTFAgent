from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend.mcp.mcp_server import SERVER_NAMES
from backend.tools.catalog import ToolCatalog
from backend.tools.tool_registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_toolchain_sync_script_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_toolchain_sync.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_catalog_doc_generator_check_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_catalog_docs.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified" in result.stdout


def test_pentest_projects_expose_web_pwn_and_reverse_tools():
    registry = ToolRegistry().load()
    exposed = registry.exposed_for("pentest")
    categories = {tool.category for tool in exposed}
    names = {tool.name for tool in exposed}

    assert categories == {"web", "pwn", "reverse"}
    assert {"pwntools", "checksec", "socat", "ropgadget"} <= names
    assert {"radare2", "objdump", "angr", "xxd"} <= names
    assert {"ffuf", "nuclei", "sqlmap", "playwright-cli"} <= names
    assert len(names) == len(exposed)
    assert {tool.category for tool in registry.exposed_for("misc")} == {"misc"}


def test_new_tools_are_present_and_old_tools_are_absent():
    registry = ToolRegistry().load()
    names = {tool.name for tool in registry.all_tools()}
    for required in ("playwright-cli", "flask-pin", "upx", "tshark",
                     "strace", "gdbserver", "foremost", "elfutils", "socat"):
        assert required in names
    for removed in ("browser", "reverse", "ghidra", "fenjing", "typhonbreaker",
                    "php-filter-chain", "jndimap", "shiro550", "rsactftool",
                    "rsa-sign2n", "sage", "flask-unsign", "flask-session-cookie",
                    "blindwatermark"):
        assert removed not in names

    catalog = ToolCatalog.load()
    ids = {entry.id for entry in catalog.entries()}
    for required in ("tool_playwright_cli", "tool_flask_pin", "tool_upx",
                     "tool_tshark", "tool_strace", "tool_gdbserver",
                     "tool_foremost", "tool_elfutils", "tool_socat",
                     "core_wordlists", "lib_jwt", "lib_scapy"):
        assert required in ids
    for removed in ("lang_rust", "lang_go", "mcp_browser", "mcp_reverse",
                    "lib_pyghidra", "lib_playwright", "lib_typhonbreaker",
                    "tool_flask_unsign", "tool_flask_session_cookie",
                    "tool_rsactftool", "tool_rsa_sign2n", "tool_sage",
                    "tool_blindwatermark"):
        assert removed not in ids

    assert SERVER_NAMES == ("memory", "tool_search", "tools")
