"""Copy the sources the hosted TSecBench image needs into a build context.

The hosted image (see IPC_TsecBench/Dockerfile.hosted) is built from a separate
directory so the 3 GB upload never sees tests, data or git history. This script
keeps that directory honest: it mirrors the source tree, removes files that no
longer exist upstream, and refuses to carry a live backend/config/config.yaml
because that file holds real API keys.

    python scripts/sync_tsecbench_bundle.py D:/Desktop/Codex/IPC_CTFAgent/IPC_TsecBench
    python scripts/sync_tsecbench_bundle.py <target> --check   # drift only

--check exits 1 when the target differs from the source, which is what a build
script should gate on before producing an image.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TREES = ("backend", "frontend")
FILES = ("pyproject.toml", "alembic.ini", "LICENSE")
SKIP_DIRECTORIES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_NAMES = {"config.yaml", ".DS_Store", "Thumbs.db"}


def wanted_files(source: Path) -> dict[Path, Path]:
    """Map each relative path the bundle needs to its source path."""

    selected: dict[Path, Path] = {}
    for name in FILES:
        candidate = source / name
        if candidate.is_file():
            selected[Path(name)] = candidate
    for tree in TREES:
        root = source / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if SKIP_DIRECTORIES.intersection(relative.parts):
                continue
            if path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES:
                continue
            selected[relative] = path
    return selected


def stale_files(target: Path, selected: dict[Path, Path]) -> list[Path]:
    """Files under the mirrored trees that no longer exist in the source."""

    stale: list[Path] = []
    for tree in TREES:
        root = target / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.relative_to(target) not in selected:
                stale.append(path)
    return stale


TEXT_SUFFIXES = frozenset(
    {".sh", ".py", ".md", ".toml", ".ini", ".yml", ".yaml", ".txt", ".cfg"}
)


def normalise(relative: Path, content: bytes) -> bytes:
    if relative.suffix.lower() in TEXT_SUFFIXES and b"\r\n" in content:
        return content.replace(b"\r\n", b"\n")
    return content


def sync(source: Path, target: Path, *, check: bool) -> list[str]:
    selected = wanted_files(source)
    changes: list[str] = []
    for relative, path in sorted(selected.items()):
        destination = target / relative
        content = normalise(relative, path.read_bytes())
        if destination.is_file() and destination.read_bytes() == content:
            continue
        changes.append("update " + str(relative))
        if not check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    for path in stale_files(target, selected):
        relative = path.relative_to(target)
        changes.append("delete " + str(relative))
        if not check:
            path.unlink()
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the hosted TSecBench build context.")
    parser.add_argument("target", type=Path, help="bundle directory, e.g. .../IPC_TsecBench")
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)

    source = args.source.resolve()
    target = args.target.resolve()
    if not (source / "backend").is_dir():
        print("not an IPC checkout: " + str(source), file=sys.stderr)
        return 2
    if target == source:
        print("target must differ from the source checkout", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)

    changes = sync(source, target, check=args.check)
    for change in changes:
        print(change)
    if not changes:
        print("bundle is up to date")
        return 0
    if args.check:
        print(str(len(changes)) + " file(s) drifted from the checkout", file=sys.stderr)
        return 1
    print(str(len(changes)) + " file(s) synced into " + str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
