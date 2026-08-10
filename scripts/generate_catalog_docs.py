"""Regenerate the packaged Memory tool-catalog Markdown documents."""

from __future__ import annotations

from pathlib import Path

from backend.tools.catalog import CATALOG_DOCS_PATH, ToolCatalog


def main() -> None:
    catalog = ToolCatalog.load(validate_documents=False)
    CATALOG_DOCS_PATH.mkdir(parents=True, exist_ok=True)
    expected: set[Path] = set()
    for entry in catalog.entries():
        destination = CATALOG_DOCS_PATH / f"{entry.id}.md"
        destination.write_text(catalog.render_document(entry.id), encoding="utf-8")
        expected.add(destination.resolve())

    for stale in CATALOG_DOCS_PATH.glob("*.md"):
        if stale.resolve() not in expected:
            stale.unlink()
    print(f"generated {len(expected)} catalog documents")


if __name__ == "__main__":
    main()
