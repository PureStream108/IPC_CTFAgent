from __future__ import annotations

from pathlib import Path

from backend.tools.catalog import ToolCatalog
from backend.tools.tool_registry import LANGUAGES

ROOT = Path(__file__).resolve().parents[1]


def test_task_image_policy_no_removed_packages_or_mcp_servers():
    dockerfile = (ROOT / "docker/member/Dockerfile").read_text(encoding="utf-8")
    for banned in ("rustc", "cargo", "pyghidra", "fenjing"):
        for line in dockerfile.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if banned not in line.casefold():
                continue
            assert any(word in line.casefold() for word in (
                "purge", "uninstall", "rm -rf", "removed", "no longer"
            )), f"active install of {banned}: {line}"
    assert "ENV IPC_BROWSER" not in dockerfile
    assert "PLAYWRIGHT_CLI_VERSION=0.1.22" in dockerfile
    assert "NODE_VERSION=22.23.4" in dockerfile
    # UPX is now the Kali distro build: it must come from apt, not an upstream
    # release tarball, and the image still proves the 4.x build at build time.
    apt_line = next(
        line for line in dockerfile.splitlines()
        if "upx-ucl" in line and "purge" not in line and not line.lstrip().startswith("#")
    )
    assert "apt-install-retry" in dockerfile[: dockerfile.index(apt_line)]
    assert "upx --version" in dockerfile
    assert "UPX_VERSION" not in dockerfile


def test_registry_and_catalog_do_not_expose_removed_runtime():
    catalog = ToolCatalog.load()
    ids = {entry.id for entry in catalog.entries()}
    assert "lib_playwright" not in ids
    assert "lib_pyghidra" not in ids
    assert "mcp_browser" not in ids
    assert "mcp_reverse" not in ids
    assert "rust" not in LANGUAGES
    for yaml_path in (ROOT / "backend/tools/registry").glob("*.yaml"):
        text = yaml_path.read_text(encoding="utf-8")
        assert "mcp:browser" not in text
        assert "mcp:reverse" not in text
        assert "name: browser" not in text
        assert "name: ghidra" not in text
