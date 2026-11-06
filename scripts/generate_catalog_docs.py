from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.tools.catalog import CATALOG_DOCS_PATH, ToolCatalog

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


def _missing_sections(path: Path) -> list[str]:
    if not path.is_file():
        return list(REQUIRED_SECTIONS)
    source = path.read_text(encoding="utf-8")
    return [section for section in REQUIRED_SECTIONS if section not in source]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify catalog documents")
    parser.add_argument("--check", action="store_true", help="verify only; never write files")
    args = parser.parse_args(argv)

    catalog = ToolCatalog.load(validate_documents=False)
    expected = {f"{entry.id}.md" for entry in catalog.entries()}

    if args.check:
        problems: list[str] = []
        for entry in catalog.entries():
            document = CATALOG_DOCS_PATH / f"{entry.id}.md"
            missing = _missing_sections(document)
            if missing:
                problems.append(f"{document.name}: missing sections: {', '.join(missing)}")
        for stale in sorted(CATALOG_DOCS_PATH.glob("*.md")):
            if stale.name not in expected:
                problems.append(f"{stale.name}: stale document for an unknown catalog entry")
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}")
            return 1
        print(f"verified {len(expected)} catalog documents")
        return 0

    CATALOG_DOCS_PATH.mkdir(parents=True, exist_ok=True)
    created = 0
    for entry in catalog.entries():
        destination = CATALOG_DOCS_PATH / f"{entry.id}.md"
        if destination.exists():
            continue
        destination.write_text(catalog.render_document(entry.id), encoding="utf-8")
        created += 1

    for stale in CATALOG_DOCS_PATH.glob("*.md"):
        if stale.name not in expected:
            stale.unlink()
    print(f"catalog documents: {len(expected)} total, {created} created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
