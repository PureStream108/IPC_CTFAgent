from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.mcp.mcp_server import SERVER_NAMES
from backend.tools.catalog import CATALOG_DOCS_PATH, ToolCatalog
from backend.tools.tool_registry import ToolRegistry

REQUIRED_SECTIONS = (
    "## 用途与适用场景",
    "## 版本检查",
    "## 命令、导入与镜像路径",
    "## 常用工作流",
    "## 可执行示例",
    "## 输出解释",
    "## 常见错误与限制",
    "## 关联条目",
    "## 官方参考",
)

REQUIRED_DOCKER = (
    "gcc", "g++", "make", "tshark", "tcpdump", "strace", "socat",
    "musl-tools", "gdbserver", "foremost", "elfutils", "upx",
    "playwright-cli", "jwcrypto", "scapy", "dpkt",
    "wordlists", "rockyou", "NODE_VERSION=22.23.4",
    "PLAYWRIGHT_CLI_VERSION=0.1.22", "pyjwt",
)
REQUIRED_MEMBER = (
    "playwright-cli", "Flask PIN helper", "upx", "tshark", "strace",
    "gdbserver", "foremost", "elfutils", "socat", "wordlists",
    "PyJWT", "scapy", "musl-gcc", "gcc", "g++", "make",
)
REQUIRED_CATALOG = (
    "core_wordlists", "lib_jwt", "lib_scapy", "tool_playwright_cli",
    "tool_flask_pin", "tool_upx", "tool_tshark", "tool_strace",
    "tool_gdbserver", "tool_foremost", "tool_elfutils", "tool_socat",
    "tool_file", "tool_xxd",
)
REMOVED_CATALOG = (
    "lang_rust", "lang_go", "mcp_browser", "mcp_reverse", "lib_pyghidra",
    "lib_playwright", "lib_typhonbreaker", "tool_flask_unsign",
    "tool_flask_session_cookie", "tool_rsactftool", "tool_rsa_sign2n",
    "tool_sage", "tool_blindwatermark",
)
BANNED_ACTIVE = (
    "browser mcp", "reverse mcp", "pyghidra", "ghidra", "rust/cargo",
    "fenjing", "typhonbreaker", "php_filter_chain_generator", "jndimap",
    # upx-ucl is no longer banned: Kali rolling ships UPX 4.x (Ubuntu 22.04
    # shipped 3.96, which is why the image used to build it from upstream).
    "shiroattack2", "--require-browser",
)
REMOVAL_CONTEXT = re.compile(
    r"没有|已移除|移除|不再|未安装|not installed|removed|no longer|without|"
    r"purge|uninstall|rm -rf|deprecated|no .*mcp",
    re.IGNORECASE,
)
_DOC_LINK_RE = re.compile(r"/memory/catalog/([a-z0-9][a-z0-9_-]*)/document")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _error(errors: list[str], message: str) -> None:
    errors.append(message)


def _check_banned(errors: list[str], label: str, text: str, *, skip_comments: bool) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        if skip_comments and line.lstrip().startswith("#"):
            continue
        if REMOVAL_CONTEXT.search(line):
            continue
        lowered = line.casefold()
        for token in BANNED_ACTIVE:
            if token in lowered:
                _error(errors, f"{label}:{number}: banned legacy token {token!r}")


def _check_documents(errors: list[str], catalog: ToolCatalog) -> None:
    for entry in catalog.entries():
        document = CATALOG_DOCS_PATH / f"{entry.id}.md"
        if not document.is_file():
            _error(errors, f"catalog doc missing: {document.name}")
            continue
        markdown = _read(document)
        if not markdown.startswith(f"# {entry.title}\n"):
            _error(errors, f"catalog doc title mismatch: {document.name}")
        for section in REQUIRED_SECTIONS:
            if section not in markdown:
                _error(errors, f"catalog doc {document.name} missing {section}")
        linked = set(_DOC_LINK_RE.findall(markdown))
        ids = {item.id for item in catalog.entries()}
        if linked - ids:
            _error(errors, f"catalog doc {document.name} links unknown ids: {sorted(linked - ids)}")


def check(root: Path | None = None) -> list[str]:
    root = root or ROOT
    errors: list[str] = []

    dockerfile = _read(root / "docker/member/Dockerfile")
    for token in REQUIRED_DOCKER:
        if token not in dockerfile:
            _error(errors, f"Dockerfile missing required token {token!r}")
    _check_banned(errors, "Dockerfile", dockerfile, skip_comments=True)

    catalog = ToolCatalog.load()
    ids = {entry.id for entry in catalog.entries()}
    for token in REQUIRED_CATALOG:
        if token not in ids:
            _error(errors, f"catalog missing required entry {token!r}")
    for token in REMOVED_CATALOG:
        if token in ids:
            _error(errors, f"catalog still exposes removed entry {token!r}")
    _check_documents(errors, catalog)

    registry = ToolRegistry().load()
    for tool in registry.all_tools():
        entry_id = "tool_" + re.sub(r"[^a-z0-9]+", "_", tool.name.lower()).strip("_")
        entry = catalog.get(entry_id) or next(
            (item for item in catalog.entries() if item.title.casefold() == tool.name.casefold()),
            None,
        )
        if entry is None:
            _error(errors, f"registry tool {tool.name!r} has no catalog entry")
        if tool.exec.startswith("mcp:"):
            _error(errors, f"registry tool {tool.name!r} still points at an MCP transport")

    if SERVER_NAMES != ("memory", "tool_search", "tools"):
        _error(errors, f"unexpected SERVER_NAMES: {SERVER_NAMES}")

    member_tools = _read(root / "backend/tools/member_tools.txt")
    for token in REQUIRED_MEMBER:
        if token not in member_tools:
            _error(errors, f"member_tools.txt missing required token {token!r}")
    _check_banned(errors, "member_tools.txt", member_tools, skip_comments=False)

    for rel in ("README.md", "docs/pentest.md"):
        _check_banned(errors, rel, _read(root / rel), skip_comments=False)

    for document in sorted(CATALOG_DOCS_PATH.glob("*.md")):
        _check_banned(errors, f"catalog_docs/{document.name}", _read(document), skip_comments=False)

    return errors


def main() -> int:
    errors = check()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("toolchain sync checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
