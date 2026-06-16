from __future__ import annotations

import yaml

from backend.blackboard.models import ProjectDetail


def export_yaml(detail: ProjectDetail) -> str:
    origin = next((f.description for f in detail.facts if f.id == "origin"), "")
    goal = next((f.description for f in detail.facts if f.id == "goal"), "")
    data: dict = {
        "project": {"title": detail.project.title, "category": detail.project.category,
                    "origin": origin, "goal": goal}
    }
    if detail.hints:
        data["hints"] = [{"content": h.content, "creator": h.creator} for h in detail.hints]
    data["facts"] = [{"id": f.id, "description": f.description} for f in detail.facts]
    if detail.intents:
        data["intents"] = [
            {"from": i.from_, "to": i.to, "description": i.description,
             "creator": i.creator, "worker": i.worker}
            for i in detail.intents
        ]
    if detail.reports:
        data["reports"] = [
            {"member": r.member, "difficulty": r.difficulty, "progress": r.progress,
             "directions": r.directions, "knowledge": r.knowledge}
            for r in detail.reports
        ]
    return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def build_timeline(detail: ProjectDetail) -> list[dict]:
    events: list[dict] = []
    p = detail.project
    events.append({"ts": p.created_at, "kind": "project_created",
                   "label": p.title, "detail": f"category={p.category}", "order": 0})

    facts_by_id = {f.id: f.description for f in detail.facts}

    for a in detail.agents:
        if a.role == "member" and a.created_at:
            events.append({"ts": a.created_at, "kind": "member_joined",
                           "label": a.name, "detail": f"start={a.start_fact_id or 'origin'}", "order": 1})

    for i in detail.intents:
        events.append({"ts": i.created_at, "kind": "intent_declared",
                       "label": i.id, "detail": i.description, "order": 2})
        if i.concluded_at and i.to:
            if i.to == "goal":
                events.append({"ts": i.concluded_at, "kind": "flag_found",
                               "label": p.flag or "", "detail": i.description, "order": 3})
            else:
                events.append({"ts": i.concluded_at, "kind": "intent_concluded",
                               "label": i.id, "detail": facts_by_id.get(i.to, ""), "order": 3})

    for r in detail.reports:
        events.append({"ts": r.created_at, "kind": "difficulty_report",
                       "label": r.member, "detail": f"{r.difficulty}: {r.progress}", "order": 2})

    if p.wp_path:
        events.append({"ts": p.updated_at, "kind": "wp_written", "label": p.wp_path, "detail": "", "order": 4})
    if p.status == "completed":
        events.append({"ts": p.updated_at, "kind": "completed", "label": p.title,
                       "detail": p.flag or "", "order": 5})

    events.sort(key=lambda e: (e["ts"], e["order"]))
    return events
