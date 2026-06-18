from __future__ import annotations

from dataclasses import dataclass

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.config import AppConfig, MemberConfig
from backend.core.difficulty import extra_members_for_difficulty, normalize_difficulty
from backend.core.logging_util import IPCLogger
from backend.core.wp_writer import write_wp

BOOTSTRAP_DESC = "Bootstrap: Starting"


@dataclass
class Assignment:
    member: str
    intent_id: str
    is_initial: bool = False


class Diamond:
    def __init__(self, db, config: AppConfig, logger: IPCLogger):
        self.db = db
        self.config = config
        self.logger = logger

    # ---- member availability ----

    def available_member_configs(self) -> list[MemberConfig]:
        return self.config.available_members()

    def _idle_members(self, project_id: str) -> list[MemberConfig]:
        with self.db.connect() as conn:
            active = set(graph_store.active_member_names(conn, project_id))
        return [m for m in self.available_member_configs() if m.name not in active]

    # ---- initial deployment ----

    def assign_initial(self, project_id: str) -> Assignment | None:
        avail = self.available_member_configs()
        if not avail:
            return None
        # Aventurine is the default first worker by config convention only; all
        # Members are equal CTF solvers and reinforcements use the same class.
        initial = next((m for m in avail if m.name == "aventurine"), avail[0])
        with self.db.connect() as conn:
            graph_store.set_agent_state(conn, project_id, "diamond", "active")
            # IPC -> Diamond (start solving)
            if not graph_store.link_exists(conn, project_id, "ipc", "diamond", "start"):
                graph_store.add_link(conn, project_id, "ipc", "diamond", "start")
            intent = edge_store.create_intent(conn, project_id, ["origin"], BOOTSTRAP_DESC, "diamond")
            graph_store.add_agent(conn, project_id, initial.name, "member", state="active", start_fact_id="origin")
            graph_store.set_agent_state(conn, project_id, initial.name, "active")
            graph_store.add_link(conn, project_id, "diamond", initial.name, "assign")
            graph_store.add_link(conn, project_id, initial.name, f"intent:{intent.id}", "explore")
        self.logger.project("diamond_assign_initial", project_id, member=initial.name, intent=intent.id)
        return Assignment(member=initial.name, intent_id=intent.id, is_initial=True)

    # ---- reinforcements on report ----

    def decide_reinforcements(self, project_id: str, report, available_slots: int | None = None) -> list[Assignment]:
        """On a difficulty report, add 1..N idle members on fresh directions."""
        difficulty = normalize_difficulty(report.difficulty)
        want = extra_members_for_difficulty(difficulty)
        if want <= 0:
            self.logger.project("diamond_no_reinforce", project_id, reason="low difficulty")
            return []
        idle = self._idle_members(project_id)
        if not idle:
            return []
        want = min(want, self.config.runtime.max_members_per_report, len(idle))
        if available_slots is not None:
            want = min(want, max(0, available_slots))
            if want <= 0:
                self.logger.project("diamond_no_reinforce", project_id, reason="project_member_cap")
                return []

        directions = self._dedupe_directions(
            report.directions or [f"alternate approach to: {report.progress}"]
        )
        start_node = report.node_id or "origin"

        assignments: list[Assignment] = []
        with self.db.connect() as conn:
            if not node_store.fact_exists(conn, project_id, start_node):
                start_node = "origin"
            for member, direction in zip(idle[:want], directions[:want]):
                existing = edge_store.find_similar_open_intent(conn, project_id, [start_node], direction)
                if existing is not None:
                    self.logger.project(
                        "diamond_intent_deduped",
                        project_id,
                        existing_intent=existing.id,
                        source=start_node,
                        direction=direction,
                    )
                    continue
                intent = edge_store.create_intent(conn, project_id, [start_node], direction, "diamond")
                graph_store.add_agent(conn, project_id, member.name, "member", state="active", start_fact_id=start_node)
                graph_store.add_link(conn, project_id, "diamond", member.name, "assign")
                graph_store.add_link(conn, project_id, member.name, f"intent:{intent.id}", "explore")
                assignments.append(Assignment(member=member.name, intent_id=intent.id))
        if not assignments:
            self.logger.project("diamond_no_reinforce", project_id, reason="no_fresh_directions")
            return []
        self.logger.project(
            "diamond_reinforce", project_id, count=len(assignments),
            members=[a.member for a in assignments], difficulty=difficulty,
        )
        return assignments

    def _dedupe_directions(self, directions: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for direction in directions:
            text = (direction or "").strip()
            key = edge_store.normalize_intent_description(text)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(text)
        return unique

    # ---- checkpoint-gated re-planning ----

    def plan_next_intent(self, project_id: str, detail, trigger: str):
        """Create one follow-up intent after a real graph-state checkpoint change."""
        open_intents = [intent for intent in detail.intents if intent.to is None]
        if open_intents:
            self.logger.project(
                "diamond_reason_noop",
                project_id,
                reason="open_intents_exist",
                trigger=trigger,
            )
            return None
        facts = [fact for fact in detail.facts if fact.id != "goal"]
        source = facts[-1].id if facts else "origin"
        latest = facts[-1].description if facts else ""
        goal = next((fact.description for fact in detail.facts if fact.id == "goal"), "")
        direction = (
            "Continue from the latest confirmed fact and choose the next concrete exploit or analysis step "
            f"toward the goal. Latest fact: {latest[:240]} Goal: {goal[:160]}"
        )
        with self.db.connect() as conn:
            if not node_store.fact_exists(conn, project_id, source):
                source = "origin"
            existing = edge_store.find_similar_open_intent(conn, project_id, [source], direction)
            if existing is not None:
                self.logger.project(
                    "diamond_reason_deduped",
                    project_id,
                    existing_intent=existing.id,
                    source=source,
                    trigger=trigger,
                )
                return None
            intent = edge_store.create_intent(conn, project_id, [source], direction, "diamond")
        self.logger.project(
            "diamond_reason_intent",
            project_id,
            intent=intent.id,
            source=source,
            trigger=trigger,
        )
        return intent

    # ---- closing the project ----

    def write_wp(self, project_id: str, wp_dir) -> str:
        """Diamond writes the final Chinese WP/EXP from the confirmed graph state."""
        path = write_wp(self.db, project_id, wp_dir)
        self.logger.project("diamond_wp_written", project_id, path=path)
        return path

    def draw_completion(self, project_id: str) -> None:
        """Draw the WP -> Diamond -> IPC return lines after flag (Seed.md 流程图)."""
        with self.db.connect() as conn:
            if not graph_store.link_exists(conn, project_id, "flag", "wp", "wp"):
                graph_store.add_link(conn, project_id, "flag", "wp", "wp")
            if not graph_store.link_exists(conn, project_id, "wp", "diamond", "wp"):
                graph_store.add_link(conn, project_id, "wp", "diamond", "wp")
            if not graph_store.link_exists(conn, project_id, "diamond", "ipc", "return"):
                graph_store.add_link(conn, project_id, "diamond", "ipc", "return")
            graph_store.set_agent_state(conn, project_id, "diamond", "done")
            for name in graph_store.active_member_names(conn, project_id):
                graph_store.set_agent_state(conn, project_id, name, "done")
