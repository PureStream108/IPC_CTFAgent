from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.logging_util import IPCLogger
from backend.mcp.base import MCPRegistry
from backend.members.adapters import BaseAdapter, MemberAction
from backend.memory.memory_search import search as mem_search
from backend.memory.memory_store import MemoryStore
from backend.sandbox.sandbox import Sandbox
from backend.tools.tool_registry import ToolRegistry


@dataclass
class MemberDeps:
    db: Any
    logger: IPCLogger
    sandbox: Sandbox
    mcps: MCPRegistry
    registry: ToolRegistry
    memory: MemoryStore
    eval_interval: int = 7
    max_steps: int = 60
    max_actions_per_task: int = 4
    on_report: Callable[[str, Any], None] | None = None   # (project_id, Report)
    on_flag: Callable[[str], None] | None = None           # (project_id)
    expected_flag: str | None = None


@dataclass
class SolveResult:
    status: str          # concluded | flag | done | stalled | failed | stopped
    steps: int
    fact_id: str | None = None
    flag: str | None = None


@dataclass
class DispatchResult:
    result: SolveResult | None = None
    graph_action: str | None = None


class BaseMember:
    role_blurb = "a versatile CTF solver"

    def __init__(self, name: str, adapter: BaseAdapter, deps: MemberDeps):
        self.name = name
        self.adapter = adapter
        self.deps = deps
        self._stop = threading.Event()
        self.observations: list[str] = []
        self._last_action_sig: str | None = None
        self._same_action_streak: int = 0

    def stop(self) -> None:
        self._stop.set()

    # ---- main loop ----

    def solve(self, project_id: str, intent_id: str, category: str, is_initial: bool = False) -> SolveResult:
        d = self.deps
        d.logger.project("member_start", project_id, member=self.name, intent=intent_id, initial=is_initial)
        self._claim(project_id, intent_id)
        step = 0
        task_budget = max(1, min(d.max_steps, d.max_actions_per_task))
        graph_actions: list[str] = []
        branch_intents = 0
        while step < task_budget and not self._stop.is_set():
            step += 1
            evaluate_now = step % d.eval_interval == 0
            context = self._build_context(project_id, intent_id, category, step, is_initial, evaluate_now)
            try:
                action = self.adapter.decide(context)
            except Exception as exc:
                d.logger.project("member_error", project_id, member=self.name, error=str(exc))
                self._release(project_id, intent_id)
                return SolveResult(status="failed", steps=step)
            d.logger.llm("decide", project_id, member=self.name, step=step,
                         thought=action.thought, action=action.kind)
            self._heartbeat(project_id, intent_id)
            if self._record_action_signature(action):
                self._submit_stall_report(project_id, intent_id, category, step)
                self._release(project_id, intent_id)
                d.logger.project("member_loop_detected", project_id, member=self.name, intent=intent_id, steps=step)
                return SolveResult(status="stalled", steps=step)

            dispatched = self._dispatch(
                project_id,
                intent_id,
                category,
                action,
                step,
                allow_intent=branch_intents < 1,
            )
            if dispatched.graph_action is not None:
                graph_actions.append(dispatched.graph_action)
                if dispatched.graph_action == "intent":
                    branch_intents += 1
            if dispatched.result is not None:
                return dispatched.result
        if self._stop.is_set():
            self._release(project_id, intent_id)
            d.logger.project("member_stopped", project_id, member=self.name, intent=intent_id, steps=step)
            return SolveResult(status="stopped", steps=step)

        if not graph_actions:
            self._submit_stall_report(project_id, intent_id, category, step)
        self._release(project_id, intent_id)
        status = "done" if graph_actions else "stalled"
        d.logger.project(
            "member_task_finished",
            project_id,
            member=self.name,
            intent=intent_id,
            status=status,
            steps=step,
            graph_actions=graph_actions,
        )
        return SolveResult(status=status, steps=step)

    # ---- action dispatch ----

    def _dispatch(
        self,
        project_id,
        intent_id,
        category,
        action: MemberAction,
        step,
        *,
        allow_intent: bool,
    ) -> DispatchResult:
        d = self.deps
        kind = action.kind
        if kind == "bash":
            cmd = action.args.get("command", "")
            res = d.sandbox.exec(cmd, timeout=60)
            self._observe(f"$ {cmd}\n{res.stdout}\n{res.stderr}".strip())
            d.logger.tool("bash", project_id, member=self.name, command=cmd, exit_code=res.exit_code)
            return DispatchResult()
        if kind == "tool":
            server = action.args.get("server", "")
            tool = action.args.get("tool", "")
            args = action.args.get("args", {})
            try:
                out = d.mcps.call(server, tool, **args)
            except Exception as exc:
                out = {"error": str(exc)}
            self._observe(f"[mcp:{server}.{tool}] {out}")
            d.logger.tool("mcp_call", project_id, member=self.name, server=server, tool=tool)
            return DispatchResult()
        if kind == "memory":
            query = action.args.get("query", "")
            hits = mem_search(d.memory, query, limit=5)
            self._observe(f"[memory:{query}] " + "; ".join(f"{m.title}" for m, _ in hits))
            d.logger.memory("search", project_id, member=self.name, query=query, hits=len(hits))
            return DispatchResult()
        if kind == "tool_search":
            query = action.args.get("query", "")
            tools = d.registry.search(query)
            self._observe(f"[tool_search:{query}] " + ", ".join(t.name for t in tools))
            d.logger.tool("tool_search", project_id, member=self.name, query=query)
            return DispatchResult()
        if kind == "report":
            self._submit_report(project_id, intent_id, action)
            return DispatchResult(graph_action="report")
        if kind == "intent":
            if not allow_intent:
                d.logger.project(
                    "intent_budget_exhausted",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    description=action.args.get("description", "explore"),
                )
                return DispatchResult()
            created = self._declare_intent(project_id, action)
            return DispatchResult(graph_action="intent" if created is not None else None)
        if kind == "conclude":
            return DispatchResult(result=self._conclude(project_id, intent_id, action), graph_action="conclude")
        if kind == "flag":
            return DispatchResult(result=self._raise_flag(project_id, intent_id, action, step), graph_action="flag")
        if kind == "done":
            self._release(project_id, intent_id)
            d.logger.project("member_done", project_id, member=self.name, reason=action.args.get("reason"))
            return DispatchResult(result=SolveResult(status="done", steps=step))
        return DispatchResult()

    # ---- blackboard ops ----

    def _claim(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            edge_store.claim_intent(conn, project_id, intent_id, self.name)

    def _heartbeat(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is not None and row["to_fact_id"] is None:
                edge_store.claim_intent(conn, project_id, intent_id, self.name)

    def _release(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is not None and row["to_fact_id"] is None and row["worker"] == self.name:
                edge_store.release_intent(conn, project_id, intent_id)

    def _submit_report(self, project_id, intent_id, action: MemberAction):
        d = self.deps
        a = action.args
        with d.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            node_id = row["to_fact_id"] if row else None
            report = graph_store.create_report(
                conn, project_id, self.name, a.get("progress", ""), a.get("difficulty", "low"),
                node_id, a.get("steps", []), a.get("directions", []), a.get("knowledge", []),
            )
            graph_store.add_link(conn, project_id, self.name, "diamond", "report")
        d.logger.project("difficulty_report", project_id, member=self.name, difficulty=report.difficulty)
        if d.on_report is not None:
            d.on_report(project_id, report)

    def _declare_intent(self, project_id, action: MemberAction):
        a = action.args
        from_ids = a.get("from") or ["origin"]
        description = a.get("description", "explore")
        with self.deps.db.connect() as conn:
            for fid in from_ids:
                if not node_store.fact_exists(conn, project_id, fid):
                    from_ids = ["origin"]
                    break
            existing = edge_store.find_similar_open_intent(conn, project_id, from_ids, description)
            if existing is not None:
                self.deps.logger.project(
                    "intent_deduped",
                    project_id,
                    member=self.name,
                    existing_intent=existing.id,
                    from_ids=from_ids,
                    description=description,
                )
                return None
            intent = edge_store.create_intent(conn, project_id, from_ids, description, self.name)
            self.deps.logger.project(
                "intent_declared",
                project_id,
                member=self.name,
                intent=intent.id,
                from_ids=from_ids,
                description=description,
            )
            return intent

    def _submit_stall_report(self, project_id, intent_id, category, step) -> None:
        recent = self.observations[-4:]
        progress = "Short exploration ended without a confirmed result."
        if recent:
            progress = "Short exploration observations:\n" + "\n\n".join(recent)
        action = MemberAction(
            kind="report",
            thought="short task budget exhausted; sharing observations for follow-up",
            args={
                "progress": progress[:1800],
                "difficulty": "low",
                "steps": recent or [f"Used {step} short-task actions on {category} intent."],
                "directions": [
                    "Try a different concrete approach for this intent.",
                    "Use sibling findings and avoid repeating the same command sequence.",
                ],
                "knowledge": [category, "short_task_stall"],
            },
        )
        self._submit_report(project_id, intent_id, action)
        self.deps.logger.project(
            "member_stalled",
            project_id,
            member=self.name,
            intent=intent_id,
            steps=step,
        )

    def _record_action_signature(self, action: MemberAction) -> bool:
        import json

        sig = action.kind + ":" + json.dumps(action.args, sort_keys=True, ensure_ascii=False)
        if sig == self._last_action_sig:
            self._same_action_streak += 1
        else:
            self._last_action_sig = sig
            self._same_action_streak = 1
        return self._same_action_streak >= 3 and action.kind in {"bash", "tool", "tool_search", "memory"}

    def _conclude(self, project_id, intent_id, action: MemberAction) -> SolveResult:
        desc = action.args.get("description", "confirmed result")
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is None or row["to_fact_id"] is not None:
                return SolveResult(status="done", steps=0)
            fact = node_store.create_fact(conn, project_id, desc)
            edge_store.conclude_intent(conn, project_id, intent_id, self.name, fact.id)
            graph_store.touch_project(conn, project_id)
        self.deps.logger.project("intent_concluded", project_id, member=self.name, intent=intent_id, fact=fact.id)
        return SolveResult(status="concluded", steps=0, fact_id=fact.id)

    def _raise_flag(self, project_id, intent_id, action: MemberAction, step) -> SolveResult:
        d = self.deps
        flag = action.args.get("flag", "")
        desc = action.args.get("description", "flag captured")
        with d.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            # ensure the assigned intent is concluded into a fact first
            if row is not None and row["to_fact_id"] is None:
                fact = node_store.create_fact(conn, project_id, desc)
                edge_store.conclude_intent(conn, project_id, intent_id, self.name, fact.id)
                from_fact = fact.id
            else:
                from_fact = row["to_fact_id"] if row else "origin"
            # completion edge -> goal
            comp = edge_store.create_intent(conn, project_id, [from_fact], desc, self.name, worker=self.name)
            conn.execute(
                "UPDATE intents SET to_fact_id='goal', concluded_at=? WHERE id=? AND project_id=?",
                (comp.created_at, comp.id, project_id),
            )
            graph_store.set_flag(conn, project_id, flag)
            graph_store.set_status(conn, project_id, "flag_found")
            graph_store.add_link(conn, project_id, f"fact:{from_fact}", "flag", "flag")
        d.logger.project("flag_found", project_id, member=self.name, flag=flag)
        if d.on_flag is not None:
            d.on_flag(project_id)
        return SolveResult(status="flag", steps=step, flag=flag, fact_id=from_fact)

    # ---- context ----

    def _build_context(self, project_id, intent_id, category, step, is_initial, evaluate_now) -> dict:
        d = self.deps
        with d.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            raise RuntimeError(f"project {project_id} not found")
        assigned = next((i for i in detail.intents if i.id == intent_id), None)
        exposed = [t.to_dict() for t in d.registry.exposed_for(category)]
        reports = detail.reports[-8:]
        sibling_insights = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
                "knowledge": r.knowledge,
            }
            for r in reports
            if r.member != self.name
        ]
        previous_attempts = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
            }
            for r in reports
            if r.member == self.name or (assigned and r.node_id in (None, assigned.to))
        ]
        return {
            "role": self.name,
            "role_blurb": self.role_blurb,
            "category": category,
            "step": step,
            "max_steps": min(d.max_steps, d.max_actions_per_task),
            "short_task": True,
            "task_contract": (
                "This is a short exploration task. Produce one clear result quickly: "
                "flag, conclude, a useful new intent, or a difficulty report with concrete next directions. "
                "Do not repeat previous attempts."
            ),
            "sandbox_backend": getattr(d.sandbox, "__class__", type(d.sandbox)).__name__,
            "runtime_notes": [
                "If sandbox_backend is LocalSandbox, use host shell-compatible commands only.",
                "If sandbox_backend is DockerSandbox, use Linux commands inside the container.",
            ],
            "evaluate_now": evaluate_now,
            "eval_interval": d.eval_interval,
            "is_initial": is_initial,
            "expected_flag": d.expected_flag,
            "goal": next((f.description for f in detail.facts if f.id == "goal"), ""),
            "assigned_intent": {"id": intent_id, "description": assigned.description if assigned else ""},
            "facts": [{"id": f.id, "description": f.description} for f in detail.facts],
            "open_intents": [
                {"id": i.id, "description": i.description}
                for i in detail.intents if i.to is None
            ],
            "exposed_tools": exposed,
            "available_mcps": d.mcps.names(),
            "attachments": [
                {"id": a.id, "filename": a.filename, "path": a.path, "created_at": a.created_at}
                for a in detail.attachments
            ],
            "recent_observations": self.observations[-6:],
            "bump_insights": sibling_insights,
            "previous_attempts": previous_attempts[-5:],
        }

    def _observe(self, text: str) -> None:
        self.observations.append(text[:2000])
