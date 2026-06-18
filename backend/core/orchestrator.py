from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from backend.blackboard import edge_store, graph_store
from backend.core.diamond import Diamond
from backend.core.ipc import verify_flag_and_wp
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.memory_writer import write_memory
from backend.core.project_manager import ProjectManager
from backend.core.resource_manager import ResourceManager
from backend.members.base_member import MemberDeps
from backend.members.factory import create_member


@dataclass(slots=True)
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    open_intent_count: int


class Orchestrator:
    def __init__(self, state, max_workers: int = 8, scripts: dict | None = None):
        self.state = state
        self.diamond = Diamond(state.db, state.config, state.logger)
        self.lifecycle = Lifecycle(state.db)
        self.resources = ResourceManager(state.limiter, state.pool)
        self.projects = ProjectManager(state.projects_dir, state.network)
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ipc-member")
        self._members: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, list] = {}
        self._task_index: dict[tuple[str, str], Any] = {}
        self._completing: set[str] = set()
        self._reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        # optional per-(project,member) scripts for deterministic tests
        self.scripts = scripts or {}

    def start(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._stop.clear()
        self._loop_thread = threading.Thread(target=self._run_loop, name="ipc-orchestrator", daemon=True)
        self._loop_thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2)
        with self._lock:
            members = [m for proj in self._members.values() for m in proj.values()]
        for m in members:
            m.stop()
        self.executor.shutdown(wait=False)

    # ---- project category helper ----

    def _category(self, project_id: str) -> str:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            return row["category"] if row else "misc"

    # ---- start solving ----

    def start_project(self, project_id: str) -> None:
        self._reconcile_resources()
        status = self.lifecycle.status(project_id)
        if status is None:
            return
        if status == "stopped":
            self.resume_project(project_id)
            return
        if status in ("running", "flag_found", "wp_writing", "memory_writing", "completed"):
            return
        self.projects.ensure_dirs(project_id)
        self.projects.start_challenge_env(project_id)
        try:
            self.lifecycle.transition(project_id, "running")
        except LifecycleError:
            pass
        assignment = self.diamond.assign_initial(project_id)
        if assignment is None:
            self.state.logger.project("no_members_available", project_id)
            return
        self.state.logger.project("project_scheduler_started", project_id, member=assignment.member, intent=assignment.intent_id)
        self._launch_member(project_id, assignment.member, assignment.intent_id, self._category(project_id), assignment.is_initial)

    def resume_project(self, project_id: str) -> None:
        self._reconcile_resources()
        status = self.lifecycle.status(project_id)
        if status is None:
            return
        if status == "running":
            self._dispatch_project(project_id)
            return
        if status != "stopped":
            return
        self.projects.ensure_dirs(project_id)
        self.projects.start_challenge_env(project_id)
        try:
            self.lifecycle.transition(project_id, "running")
        except LifecycleError:
            with self.state.db.connect() as conn:
                graph_store.set_status(conn, project_id, "running")
        with self.state.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
            if detail is None:
                return
            graph_store.set_agent_state(conn, project_id, "diamond", "active")
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            has_open = any(intent.to is None for intent in detail.intents)
            if not has_open:
                source = next((f.id for f in reversed(detail.facts) if f.id not in ("goal",)), "origin")
                edge_store.create_intent(
                    conn,
                    project_id,
                    [source],
                    "resume exploration from reopened project state",
                    "diamond",
                )
        self.state.logger.project("project_resumed", project_id)
        self._dispatch_project(project_id)

    # ---- reinforcements ----

    def handle_report(self, project_id: str, report) -> None:
        if self.lifecycle.status(project_id) != "running":
            return
        self._broadcast_bump(project_id, report)
        assignments = self.diamond.decide_reinforcements(project_id, report)
        category = self._category(project_id)
        for a in assignments:
            self._launch_member(project_id, a.member, a.intent_id, category, a.is_initial)

    def _broadcast_bump(self, project_id: str, report) -> None:
        insights = self._format_report_bump(report)
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        bumped: list[str] = []
        for member in members:
            if member.name == report.member:
                continue
            member.bump(insights)
            bumped.append(member.name)
        if bumped:
            self.state.logger.project(
                "member_bump_broadcast",
                project_id,
                source=report.member,
                targets=bumped,
                difficulty=report.difficulty,
            )

    def _format_report_bump(self, report) -> str:
        parts = [
            f"{report.member} reports difficulty={report.difficulty}.",
            f"Progress: {report.progress}",
        ]
        if report.steps:
            parts.append("Tried:\n" + "\n".join(f"- {step}" for step in report.steps[:6]))
        if report.directions:
            parts.append("Suggested next directions:\n" + "\n".join(f"- {direction}" for direction in report.directions[:6]))
        if report.knowledge:
            parts.append("Knowledge/evidence: " + ", ".join(report.knowledge[:10]))
        parts.append("Use this to switch angle; avoid repeating the same action signature or exploit class.")
        return "\n\n".join(parts)

    # ---- member execution ----

    def _member_config(self, name: str):
        for m in self.state.config.members:
            if m.name == name:
                return m
        return None

    def _launch_member(self, project_id, member_name, intent_id, category, is_initial) -> None:
        cfg = self._member_config(member_name)
        if cfg is None:
            return
        if not self.resources.can_admit_member():
            self.state.logger.project(
                "member_admission_denied",
                project_id,
                member=member_name,
                reserved_memory_gb=self.state.limiter.reserved_memory_gb,
                active_sandboxes=self.state.pool.active_keys(),
            )
            return
        sandbox = self.resources.sandbox_for(project_id, member_name)
        deps = MemberDeps(
            db=self.state.db,
            logger=self.state.logger,
            sandbox=sandbox,
            mcps=self.state.mcps,
            registry=self.state.registry,
            memory=self.state.memory,
            eval_interval=self.state.config.runtime.eval_interval_steps,
            max_steps=self.state.config.runtime.max_member_steps,
            max_actions_per_task=self.state.config.runtime.max_member_actions_per_task,
            on_report=self.handle_report,
            on_flag=self.on_flag_found,
        )
        script = self.scripts.get((project_id, member_name)) or self.scripts.get(member_name)
        member = create_member(cfg, deps, script=script)
        with self._lock:
            self._members.setdefault(project_id, {})[member_name] = member
        future = self.executor.submit(self._run_member, project_id, member, intent_id, category, is_initial)
        with self._lock:
            self._futures.setdefault(project_id, []).append(future)
            self._task_index[(project_id, intent_id)] = future

    def _run_member(self, project_id, member, intent_id, category, is_initial):
        try:
            return member.solve(project_id, intent_id, category, is_initial=is_initial)
        except Exception as exc:
            self.state.logger.project("member_crash", project_id, member=member.name, error=str(exc))
        finally:
            with self._lock:
                self._members.get(project_id, {}).pop(member.name, None)

    # ---- stop ----

    def stop_project(self, project_id: str) -> None:
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for m in members:
            m.stop()
        self.resources.release_project(project_id)
        self.projects.teardown(project_id)

    # ---- flag found -> close pipeline ----

    def on_flag_found(self, project_id: str) -> None:
        with self._lock:
            if project_id in self._completing:
                return
            self._completing.add(project_id)
        try:
            self._finalize(project_id)
        finally:
            with self._lock:
                self._completing.discard(project_id)

    def _finalize(self, project_id: str) -> None:
        state = self.state
        # stop other members (the finder is finishing in its own thread)
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for m in members:
            m.stop()

        # status should be flag_found (member set it); ensure it
        if self.lifecycle.status(project_id) == "running":
            try:
                self.lifecycle.transition(project_id, "flag_found")
            except LifecycleError:
                pass

        # WP_WRITING
        try:
            self.lifecycle.transition(project_id, "wp_writing")
        except LifecycleError:
            pass
        wp_path = self.diamond.write_wp(project_id, state.wp_dir)
        state.logger.project("wp_written", project_id, path=wp_path)

        # MEMORY_WRITING
        try:
            self.lifecycle.transition(project_id, "memory_writing")
        except LifecycleError:
            pass
        written = write_memory(state.db, project_id, state.memory)
        state.logger.memory("experience_written", project_id, count=len(written))

        # IPC verification
        verdict = verify_flag_and_wp(state.db, project_id, state.wp_dir)
        if not verdict["ok"]:
            state.logger.project("ipc_verification_failed", project_id, reasons=verdict["reasons"])
            return

        # COMPLETED
        self.diamond.draw_completion(project_id)
        try:
            self.lifecycle.transition(project_id, "completed")
        except LifecycleError:
            pass

        # broadcast + release resources
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            title = row["title"] if row else project_id
            flag = row["flag"] if row else ""
            graph_store.add_broadcast(conn, project_id, title, flag or "")
        state.logger.project("completed", project_id, flag=verdict.get("flag"))
        self.stop_project(project_id)

    # ---- scheduler loop ----

    def _run_loop(self) -> None:
        interval = max(1, self.state.config.runtime.interval)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.state.logger.project("scheduler_error", "system", error=str(exc))
            self._stop.wait(interval)

    def _tick(self) -> None:
        self._reconcile_resources()
        with self.state.db.connect() as conn:
            graph_store.expire_reason_leases(conn, self.state.config.runtime.reason_timeout)
            edge_store.expire_workers(conn, self.state.config.runtime.intent_timeout)
        self._reap_finished_futures()
        with self.state.db.connect() as conn:
            summaries = graph_store.project_summaries(conn)
        self._initialize_reason_checkpoints(summaries)
        for summary in summaries:
            if summary.status != "running":
                continue
            self._dispatch_project(summary.id)

    def _dispatch_project(self, project_id: str) -> None:
        with self._lock:
            project_tasks = {k: f for k, f in self._task_index.items() if k[0] == project_id and not f.done()}
        if len(project_tasks) >= self.state.config.runtime.max_members_per_report:
            return
        with self.state.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            return
        running = {intent_id for (pid, intent_id), fut in project_tasks.items() if pid == project_id and not fut.done()}
        open_intents = [i for i in detail.intents if i.to is None]
        category = detail.project.category
        active_members = self._members.get(project_id, {})
        claimable = [i for i in open_intents if i.id not in running]
        claimed = [
            i for i in claimable
            if i.worker is not None and i.worker in active_members
        ]
        unclaimed = [i for i in claimable if i.worker is None]
        if not claimed and not unclaimed:
            reason_trigger = self._reason_trigger(detail)
            if reason_trigger is None:
                self.state.logger.project(
                    "diamond_reason_skipped",
                    project_id,
                    reason="graph_checkpoint_unchanged",
                    facts=len(detail.facts),
                    hints=len(detail.hints),
                    open_intents=len(open_intents),
                )
                return
            reason_snapshot = detail
            created = self.diamond.plan_next_intent(project_id, reason_snapshot, reason_trigger)
            self._record_reason_checkpoint(project_id, reason_snapshot)
            if created is not None:
                self.state.logger.project(
                    "diamond_reason_planned",
                    project_id,
                    trigger=reason_trigger,
                    intent=created.id,
                )
            return
        ordered = (
            sorted(claimed, key=lambda i: (i.created_at, i.id))
            + sorted(unclaimed, key=lambda i: (i.created_at, i.id), reverse=True)
        )
        for intent in ordered:
            if intent.id in running:
                continue
            if intent.worker is not None and intent.worker not in active_members:
                continue
            if intent.worker is None and project_tasks:
                continue
            member_name = intent.worker or self._pick_idle_member(project_id)
            if member_name is None:
                continue
            self._launch_member(project_id, member_name, intent.id, category, intent.description.startswith("bootstrap"))
            return

    def _reconcile_resources(self) -> None:
        with self.state.db.connect() as conn:
            summaries = graph_store.project_summaries(conn)
        active_project_ids = {
            summary.id
            for summary in summaries
            if summary.status in ("running", "flag_found", "wp_writing", "memory_writing")
        }
        reclaimed = self.resources.reclaim_orphaned_projects(active_project_ids)
        if reclaimed:
            self.state.logger.project(
                "orphaned_project_resources_reclaimed",
                "system",
                projects=reclaimed,
            )

    def _pick_idle_member(self, project_id: str) -> str | None:
        with self._lock:
            active = set(self._members.get(project_id, {}).keys())
        for member in self.state.config.available_members():
            if member.name not in active:
                return member.name
        return None

    def _project_open_intent_count(self, detail) -> int:
        return sum(1 for intent in detail.intents if intent.to is None)

    def _initialize_reason_checkpoints(self, summaries) -> None:
        running_ids = {summary.id for summary in summaries if summary.status == "running"}
        for project_id in list(self._reason_checkpoints):
            if project_id not in running_ids:
                self._reason_checkpoints.pop(project_id, None)
        for summary in summaries:
            if summary.status != "running" or summary.id in self._reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self._reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            self.state.logger.project(
                "diamond_reason_checkpoint_initialized",
                summary.id,
                facts=summary.fact_count,
                hints=summary.hint_count,
                open_intents=open_intent_count,
            )

    def _reason_trigger(self, detail) -> str | None:
        open_intent_count = self._project_open_intent_count(detail)
        checkpoint = self._reason_checkpoints.get(detail.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(detail.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(detail.facts)}")
        if len(detail.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(detail.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        if not changes:
            return None
        return ",".join(changes)

    def _record_reason_checkpoint(self, project_id: str, detail) -> None:
        """Record the pre-reason graph snapshot, matching Cairn's checkpoint gate.

        A reason pass may create a new intent. That intent is work to dispatch, not
        a graph-state change that should immediately trigger another reason pass.
        """
        checkpoint = ReasonCheckpoint(
            fact_count=len(detail.facts),
            hint_count=len(detail.hints),
            open_intent_count=self._project_open_intent_count(detail),
        )
        self._reason_checkpoints[project_id] = checkpoint
        self.state.logger.project(
            "diamond_reason_checkpoint_updated",
            project_id,
            facts=checkpoint.fact_count,
            hints=checkpoint.hint_count,
            open_intents=checkpoint.open_intent_count,
        )

    def _reap_finished_futures(self) -> None:
        done: list[tuple[str, str, Any]] = []
        with self._lock:
            for (project_id, intent_id), future in list(self._task_index.items()):
                if future.done():
                    done.append((project_id, intent_id, future))
        for project_id, intent_id, future in done:
            try:
                result = future.result()
            except Exception as exc:
                self.state.logger.project("member_task_crash", project_id, intent=intent_id, error=str(exc))
                continue
            if result is None:
                self.state.logger.project("member_task_failed", project_id, intent=intent_id)
                with self._lock:
                    if self._task_index.get((project_id, intent_id)) is future:
                        self._task_index.pop((project_id, intent_id), None)
                continue
            if result.status == "stalled":
                self.state.logger.project("member_task_stalled", project_id, intent=intent_id, steps=result.steps)
            elif result.status == "done":
                self.state.logger.project("member_task_done", project_id, intent=intent_id, steps=result.steps)
            elif result.status == "concluded":
                self.state.logger.project("member_task_concluded", project_id, intent=intent_id, fact=result.fact_id)
            elif result.status == "flag":
                self.state.logger.project("member_task_flag", project_id, intent=intent_id, flag=result.flag)
            with self._lock:
                if self._task_index.get((project_id, intent_id)) is future:
                    self._task_index.pop((project_id, intent_id), None)

    # ---- test helper ----

    def wait(self, project_id: str, timeout: float = 30.0) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                futures = list(self._futures.get(project_id, []))
            pending = [f for f in futures if not f.done()]
            status = self.lifecycle.status(project_id)
            if not pending and status in ("completed", "stopped", "flag_found"):
                # allow finalize to settle
                if status == "completed" or self._completing == set():
                    return
            time.sleep(0.05)
