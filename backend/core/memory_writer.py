from __future__ import annotations

from backend.blackboard import graph_store
from backend.memory.memory_store import MemoryStore


def write_memory(db, project_id: str, memory: MemoryStore, diamond_adapter=None) -> list:
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    p = detail.project
    category = p.category
    written = []

    # Knowledge: the solution path distilled.
    path_facts = [
        f.description for f in detail.facts if f.id not in ("origin", "goal")
    ]
    if path_facts:
        written.append(
            memory.add(
                "knowledge",
                title=f"{p.title}: {category} solution path",
                content="Solution facts:\n- " + "\n- ".join(path_facts[:8]),
                tags=[category, "solution"],
                project_id=project_id,
            )
        )

    # Tool usage + lessons gathered from member difficulty reports.
    knowledge_points: list[str] = []
    directions: list[str] = []
    for r in detail.reports:
        knowledge_points.extend(r.knowledge)
        directions.extend(r.directions)
    if knowledge_points:
        written.append(
            memory.add(
                "tool_usage",
                title=f"{p.title}: effective approaches ({category})",
                content="Knowledge points that mattered: " + ", ".join(dict.fromkeys(knowledge_points)),
                tags=[category] + list(dict.fromkeys(knowledge_points))[:5],
                project_id=project_id,
            )
        )

    # Exploit summary keyed off the goal edge.
    goal_edge = next((i for i in detail.intents if i.to == "goal"), None)
    if goal_edge:
        written.append(
            memory.add(
                "exploit",
                title=f"{p.title}: exploit",
                content=f"Final exploit: {goal_edge.description}. Flag: {p.flag or ''}",
                tags=[category, "exploit"],
                project_id=project_id,
            )
        )

    # Lessons: course corrections suggested mid-solve.
    if directions:
        written.append(
            memory.add(
                "lessons",
                title=f"{p.title}: what to try earlier",
                content="Directions worth trying earlier next time: "
                + "; ".join(dict.fromkeys(directions)),
                tags=[category, "lessons"],
                project_id=project_id,
            )
        )

    return written
