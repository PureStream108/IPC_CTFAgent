from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backend.blackboard import graph_store
from backend.core.replay import build_timeline


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]


def write_wp(db, project_id: str, wp_dir: Path, diamond_adapter=None) -> str:
    wp_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    p = detail.project
    origin = next((f.description for f in detail.facts if f.id == "origin"), "")
    goal = next((f.description for f in detail.facts if f.id == "goal"), "")
    concluded = [i for i in detail.intents if i.concluded_at and i.to and i.to != "goal"]
    facts_by_id = {f.id: f.description for f in detail.facts}

    lines: list[str] = []
    lines.append(f"# {p.title} — Writeup")
    lines.append("")
    lines.append(f"_Category: {p.category} · Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_")
    lines.append("")
    # 1) 题目信息
    lines.append("## 题目信息 (Challenge)")
    lines.append("")
    lines.append(f"- **Origin**: {origin}")
    lines.append(f"- **Goal**: {goal}")
    if detail.attachments:
        lines.append(f"- **Attachments**: {', '.join(a.filename for a in detail.attachments)}")
    if detail.hints:
        lines.append("- **Hints**:")
        for h in detail.hints:
            lines.append(f"  - {h.content}")
    lines.append("")
    # 2) 解题过程
    lines.append("## 解题过程 (Solution Path)")
    lines.append("")
    if not concluded:
        lines.append("_No intermediate steps recorded._")
    for idx, intent in enumerate(concluded, 1):
        lines.append(f"### Step {idx}: {intent.description}")
        lines.append("")
        lines.append(f"- From: {', '.join(intent.from_)} (by {intent.worker or intent.creator})")
        lines.append(f"- Result: {facts_by_id.get(intent.to, '')}")
        if detail.attachments:
            lines.append(f"- Code/位置: see attachment(s); reproduce against {origin}")
        lines.append("")
    # difficulty reports as analysis notes
    if detail.reports:
        lines.append("### Analysis Notes")
        lines.append("")
        for r in detail.reports:
            lines.append(f"- [{r.member}] ({r.difficulty}) {r.progress}; knowledge: {', '.join(r.knowledge)}")
        lines.append("")
    # 3) Exp
    lines.append("## Exp (Exploit)")
    lines.append("")
    lines.append("```text")
    flag_edge = next((i for i in detail.intents if i.to == "goal"), None)
    if flag_edge:
        lines.append(f"# Final exploit path: {' -> '.join(flag_edge.from_)} -> goal")
        lines.append(f"# {flag_edge.description}")
    lines.append(f"FLAG = {p.flag or '<flag>'}")
    lines.append("```")
    lines.append("")
    lines.append(f"**Flag**: `{p.flag or ''}`")
    lines.append("")

    content = "\n".join(lines)
    path = wp_dir / f"{project_id}_{_safe(p.title)}.md"
    path.write_text(content, encoding="utf-8")

    with db.connect() as conn:
        graph_store.set_wp_path(conn, project_id, str(path))
    return str(path)
