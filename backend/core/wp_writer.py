from __future__ import annotations

from pathlib import Path

from backend.blackboard import graph_store


WP_PROMPT = "The CTF challenge is complete. Write a markdown writeup, and include a complete Python exploit script in the writeup."
_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')


def _safe_filename(name: str) -> str:
    cleaned = "".join("_" if c in _INVALID_FILENAME_CHARS or ord(c) < 32 else c for c in name)
    cleaned = cleaned.strip().rstrip(".")
    return (cleaned[:120].strip() or "writeup")


def _target_path(wp_dir: Path, project_id: str, title: str, existing_wp_path: str | None) -> Path:
    if existing_wp_path:
        existing = Path(existing_wp_path)
        if existing.parent == wp_dir:
            return existing
    base = _safe_filename(title)
    path = wp_dir / f"{base}.md"
    if not path.exists():
        return path
    return wp_dir / f"{base}_{project_id}.md"


def write_wp(db, project_id: str, wp_dir: Path) -> str:
    wp_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise RuntimeError(f"project {project_id} not found")

    path = _target_path(wp_dir, project_id, detail.project.title, detail.project.wp_path)
    path.write_text(WP_PROMPT + "\n", encoding="utf-8")

    with db.connect() as conn:
        graph_store.set_wp_path(conn, project_id, str(path))
    return str(path)


def write_wp_content(db, project_id: str, wp_dir: Path, content: str) -> str:
    """Persist an agent-produced Markdown writeup for a live IPC project.

    The existing :func:`write_wp` intentionally creates a starter template for
    the multi-agent lifecycle.  A Claude Code action run already has its final
    explanation, so replacing that explanation with the starter text loses the
    useful artifact.  This helper uses the same collision-safe path policy but
    writes the supplied Markdown verbatim.
    """

    if not isinstance(content, str) or not content.strip():
        raise ValueError("writeup content must be non-empty")
    if len(content) > 1_000_000:
        raise ValueError("writeup content is limited to 1,000,000 characters")

    wp_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise RuntimeError(f"project {project_id} not found")

    path = _target_path(wp_dir, project_id, detail.project.title, detail.project.wp_path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    with db.connect() as conn:
        graph_store.set_wp_path(conn, project_id, str(path))
    return str(path)
