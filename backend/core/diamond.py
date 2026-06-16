from __future__ import annotations

from dataclasses import dataclass

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.config import AppConfig, MemberConfig
from backend.core.logging_util import IPCLogger

BOOTSTRAP_DESC = "bootstrap: take the first crack at the challenge"


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

    def decide_reinforcements(self, project_id: str, report) -> list[Assignment]:
        """On a difficulty report, add 1..N idle members on fresh directions."""
        difficulty = (report.difficulty or "medium").lower()
        if difficulty in ("low", "trivial", "easy"):
            self.logger.project("diamond_no_reinforce", project_id, reason="low difficulty")
            return []
        idle = self._idle_members(project_id)
        if not idle:
            return []
        # how many: high->3, medium->1, scaled by max_members_per_report and idle count.
        want = 3 if difficulty in ("high", "hard") else (2 if difficulty == "medium" else 1)
        want = min(want, self.config.runtime.max_members_per_report, len(idle))

        directions = report.directions or [f"alternate approach to: {report.progress}"]
        start_node = report.node_id or "origin"

        assignments: list[Assignment] = []
        with self.db.connect() as conn:
            if not node_store.fact_exists(conn, project_id, start_node):
                start_node = "origin"
            for i in range(want):
                member = idle[i]
                direction = directions[i % len(directions)]
                intent = edge_store.create_intent(conn, project_id, [start_node], direction, "diamond")
                graph_store.add_agent(conn, project_id, member.name, "member", state="active", start_fact_id=start_node)
                graph_store.add_link(conn, project_id, "diamond", member.name, "assign")
                graph_store.add_link(conn, project_id, member.name, f"intent:{intent.id}", "explore")
                assignments.append(Assignment(member=member.name, intent_id=intent.id))
        self.logger.project(
            "diamond_reinforce", project_id, count=len(assignments),
            members=[a.member for a in assignments], difficulty=difficulty,
        )
        return assignments

    # ---- closing the project ----

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
