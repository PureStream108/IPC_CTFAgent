from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import sys
from time import monotonic
from typing import Any
import uuid

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.archive import archive_completed_project
from backend.core.diamond import Diamond
from backend.core.ipc import accept_verified_flag, verify_flag
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.memory_writer import write_memory
from backend.core.monitor import assess_project, escalated
from backend.core.postprocess_store import (
    claim_next_job,
    complete_job,
    enqueue_postprocess,
    fail_job,
    lock_job,
    recover_expired_jobs,
    renew_job,
)
from backend.core.project_manager import ProjectManager
from backend.core.resource_manager import ResourceManager
from backend.mcp.mcp_client import MCPClient, MCPRegistryTarget
from backend.members.base_member import MemberDeps
from backend.members.factory import create_member
from backend.sandbox.errors import SandboxStartupError
from backend.sandbox.task_sandbox import member_workdir, task_container_name
from backend.core.wp_writer import persist_validated_writeup


@dataclass(slots=True)
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    open_intent_count: int


@dataclass(slots=True)
class IntentLease:
    worker: str
    owner: str
    token: str


class PostprocessLeaseLost(RuntimeError):
    pass


class Orchestrator:
    _MEMBER_RETRY_DELAYS = (10, 30, 90, 300)
    _MAX_CONSECUTIVE_STALLS = 3
    _STALL_DEFER_SECONDS = 180
    _POSTPROCESS_LEASE_SECONDS = 120

    def __init__(self, state, max_workers: int | None = None, scripts: dict | None = None):
        self.state = state
        self.diamond = Diamond(state.db, state.config, state.logger)
        self.lifecycle = Lifecycle(state.db)
        self.resources = ResourceManager(state.limiter, state.pool)
        self.projects = ProjectManager(state.projects_dir, state.network)
        member_workers = max_workers or max(
            4,
            state.config.limits.max_concurrent_tasks
            * state.config.runtime.max_members_per_report,
        )
        self.executor = ThreadPoolExecutor(
            max_workers=member_workers,
            thread_name_prefix="ipc-member",
        )
        self.startup_executor = ThreadPoolExecutor(
            max_workers=max(1, min(state.config.limits.max_concurrent_tasks, 8)),
            thread_name_prefix="ipc-startup",
        )
        self._members: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, list] = {}
        self._task_index: dict[tuple[str, str], Any] = {}
        self._completing: set[str] = set()
        self._reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self._pending_projects: deque[str] = deque()
        self._pending_project_ids: set[str] = set()
        self._startup_project_ids: set[str] = set()
        self._startup_futures: dict[str, Any] = {}
        self._member_failure_counts: dict[tuple[str, str], int] = {}
        self._member_retry_not_before: dict[tuple[str, str], float] = {}
        self._member_stall_counts: dict[tuple[str, str], int] = {}
        self._project_leases: dict[str, str] = {}
        self._intent_leases: dict[tuple[str, str], IntentLease] = {}
        self._monitor_levels: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._postprocess_future: Any | None = None
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
        self._stop_members()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join()

        # A startup worker can create a Member, so let all running startup work
        # finish before taking the final Member snapshot.  Waiting for both
        # executors guarantees AppState may close its database pool afterwards.
        self.startup_executor.shutdown(wait=True, cancel_futures=True)
        self._stop_members()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _stop_members(self) -> None:
        with self._lock:
            members = [m for proj in self._members.values() for m in proj.values()]
        for m in members:
            m.stop()

    # ---- project category helper ----

    def _category(self, project_id: str) -> str:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            return row["category"] if row else "misc"

    def _max_running_for(self, category: str) -> int:
        # Crypto challenges are deep single-threaded problems: one agent with
        # an unlimited step budget, never a swarm.
        if category == "crypto":
            return 1
        return self.state.config.runtime.max_members_per_report

    # ---- start solving ----

    def start_project_async(self, project_id: str) -> dict[str, str | None]:
        """Queue slow Docker/bootstrap work without blocking an HTTP or MCP call."""

        if self._stop.is_set():
            raise RuntimeError("orchestrator is shutting down")

        status = self.lifecycle.status(project_id)
        if status is None:
            raise ValueError(f"project not found: {project_id}")
        if status in ("flag_found", "solved"):
            return self.runtime_status(project_id)
        if status == "running" and not self._startup_in_progress(project_id):
            with self._lock:
                already_owned = project_id in self._project_leases
            if already_owned:
                return self.runtime_status(project_id)
            if not self._ensure_project_lease(project_id):
                return self.runtime_status(project_id)
        elif not self._ensure_project_lease(project_id):
            return self.runtime_status(project_id)
        with self._lock:
            if project_id in self._startup_project_ids:
                return self.runtime_status(project_id)
            self._startup_project_ids.add(project_id)
        self._set_runtime_phase(project_id, "queued")
        try:
            future = self.startup_executor.submit(
                self._run_project_startup,
                project_id,
                status == "stopped",
            )
        except Exception as exc:
            with self._lock:
                self._startup_project_ids.discard(project_id)
            self._mark_project_startup_failure(
                project_id,
                SandboxStartupError(
                    f"startup executor rejected project: {exc}",
                    operation="schedule project startup",
                ),
                event="project_start_schedule_failed",
            )
            raise
        with self._lock:
            self._startup_futures[project_id] = future
        return self.runtime_status(project_id)

    def _run_project_startup(self, project_id: str, resume: bool) -> None:
        try:
            if self.lifecycle.status(project_id) == "running":
                self._recover_running_project(project_id)
            elif resume:
                self.resume_project(project_id)
            else:
                self.start_project(project_id)
        except Exception as exc:
            self._mark_project_startup_failure(
                project_id,
                exc,
                event="project_background_start_failed",
            )
        finally:
            with self._lock:
                self._startup_project_ids.discard(project_id)
                self._startup_futures.pop(project_id, None)

    def _ensure_project_lease(self, project_id: str) -> bool:
        with self._lock:
            token = self._project_leases.get(project_id)
        with self.state.db.connect() as connection:
            renewed = graph_store.claim_project_lease(
                connection,
                project_id,
                self.state.instance_id,
                token=token,
                timeout=max(60, self.state.config.runtime.interval * 6),
            )
        if renewed is None:
            with self._lock:
                self._project_leases.pop(project_id, None)
            return False
        with self._lock:
            self._project_leases[project_id] = renewed
        return True

    def _heartbeat_project_leases(self) -> None:
        with self._lock:
            project_ids = list(self._project_leases)
        for project_id in project_ids:
            status = self.lifecycle.status(project_id)
            if status not in ("created", "running", "flag_found"):
                self._release_project_lease(project_id)
                continue
            if not self._ensure_project_lease(project_id):
                self._fence_project_after_lease_loss(project_id)
                self.state.logger.project(
                    "project_lease_lost", project_id, instance=self.state.instance_id
                )

    def _release_project_lease(self, project_id: str) -> None:
        with self._lock:
            token = self._project_leases.pop(project_id, None)
        if token is None:
            return
        with self.state.db.connect() as connection:
            graph_store.release_project_lease(
                connection, project_id, self.state.instance_id, token
            )

    def _fence_project_after_lease_loss(self, project_id: str) -> None:
        self._remove_queued_project(project_id)
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for member in members:
            member.stop()
        self._release_owned_intent_leases(project_id)
        self.resources.release_project(project_id)
        self.projects.teardown(project_id)

    def _release_owned_intent_leases(self, project_id: str) -> None:
        with self._lock:
            for key in [key for key in self._intent_leases if key[0] == project_id]:
                self._intent_leases.pop(key, None)
        owner_prefix = f"{self.state.instance_id}:"
        with self.state.db.connect() as connection:
            connection.execute(
                "UPDATE intents SET worker = NULL, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, last_heartbeat_at = NULL "
                "WHERE project_id = %s AND concluded_at IS NULL "
                "AND left(lease_owner, length(%s)) = %s",
                (project_id, owner_prefix, owner_prefix),
            )

    def _heartbeat_intent_leases(self) -> None:
        with self._lock:
            leases = list(self._intent_leases.items())
            owned_projects = set(self._project_leases)
        for (project_id, intent_id), lease in leases:
            with self._lock:
                future = self._task_index.get((project_id, intent_id))
            if future is not None and future.done():
                self._release_member_assignment(
                    project_id,
                    intent_id,
                    lease.owner,
                    lease.token,
                )
                continue
            if project_id not in owned_projects:
                self._release_member_assignment(
                    project_id,
                    intent_id,
                    lease.owner,
                    lease.token,
                )
                with self._lock:
                    member = self._members.get(project_id, {}).get(lease.worker)
                if member is not None:
                    member.stop()
                continue
            with self.state.db.connect() as connection:
                renewed = edge_store.claim_intent(
                    connection,
                    project_id,
                    intent_id,
                    lease.worker,
                    lease_owner=lease.owner,
                    lease_token=lease.token,
                    timeout=self.state.config.runtime.intent_timeout,
                )
                row = (
                    edge_store.get_intent(connection, project_id, intent_id)
                    if renewed is None
                    else None
                )
            if renewed is not None:
                continue
            with self._lock:
                current = self._intent_leases.get((project_id, intent_id))
                if current is not None and current.token == lease.token:
                    self._intent_leases.pop((project_id, intent_id), None)
                member = self._members.get(project_id, {}).get(lease.worker)
            if row is None or row["concluded_at"] is not None or row["lease_owner"] is None:
                continue
            if member is not None:
                member.stop()
            self.state.logger.project(
                "intent_lease_lost",
                project_id,
                intent=intent_id,
                member=lease.worker,
            )

    def _mark_project_startup_failure(
        self,
        project_id: str,
        error: BaseException,
        *,
        event: str,
    ) -> str:
        """Fence a failed startup without ever downgrading a discovered/verified flag."""

        infrastructure = isinstance(error, SandboxStartupError)
        target = "infra_error" if infrastructure else "failed"
        phase = "infra_degraded" if infrastructure else "failed"
        kind = getattr(error, "kind", "solver_reasoning")
        detail = f"{type(error).__name__}: {error}"[:2000]
        changed = False
        preserved_status: str | None = None
        with self.state.db.connect() as connection:
            row = graph_store.get_project_row(connection, project_id)
            if row is not None:
                preserved_status = str(row["status"])
                if preserved_status not in ("flag_found", "solved"):
                    changed = (
                        preserved_status != target
                        or row["runtime_phase"] != phase
                        or row["runtime_error"] != detail
                    )
                    connection.execute(
                        "UPDATE projects SET status = %s, terminal_reason = %s, "
                        "runtime_phase = %s, runtime_error = %s, updated_at = now() "
                        "WHERE id = %s AND status NOT IN ('flag_found', 'solved')",
                        (target, detail, phase, detail, project_id),
                    )
                connection.execute(
                    "UPDATE intents SET worker = NULL, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, last_heartbeat_at = NULL "
                    "WHERE project_id = %s AND concluded_at IS NULL "
                    "AND left(lease_owner, length(%s)) = %s",
                    (
                        project_id,
                        f"{self.state.instance_id}:",
                        f"{self.state.instance_id}:",
                    ),
                )
                connection.execute(
                    "UPDATE agents SET state = 'idle' "
                    "WHERE project_id = %s AND role = 'member'",
                    (project_id,),
                )
        with self._lock:
            for key in [key for key in self._intent_leases if key[0] == project_id]:
                self._intent_leases.pop(key, None)

        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for member in members:
            member.stop()
        self.resources.release_project(project_id)
        self.projects.teardown(project_id)
        self._release_project_lease(project_id)
        if changed:
            self.state.logger.project(
                event,
                project_id,
                status=target,
                error_kind=kind,
                error=detail,
            )
        elif preserved_status in ("flag_found", "solved"):
            self.state.logger.project(
                f"{event}_flag_preserved",
                project_id,
                status=preserved_status,
                error_kind=kind,
                error=detail,
            )
        return target

    def runtime_status(self, project_id: str) -> dict[str, str | None]:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise ValueError(f"project not found: {project_id}")
        return {
            "project_id": project_id,
            "status": str(row["status"]),
            "phase": str(row["runtime_phase"] or "idle"),
            "error": row["runtime_error"],
        }

    def _set_runtime_phase(
        self,
        project_id: str,
        phase: str,
        *,
        error: str | None = None,
    ) -> None:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row is None:
                return
            changed = row["runtime_phase"] != phase or row["runtime_error"] != error
            graph_store.set_runtime_phase(conn, project_id, phase, error)
        if changed:
            self.state.logger.project(
                "project_runtime_phase",
                project_id,
                phase=phase,
                error=error,
            )

    def reload_config(self) -> None:
        """Apply model configuration changes to future dispatches and WP generation.

        Active Members keep their already-created adapters. Diamond reads this shared config for
        each new assignment and final writeup, so settings saved from the UI take effect without a
        process restart or disrupting an in-flight task.
        """

        self.diamond.config = self.state.config

    def _startup_in_progress(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._startup_project_ids

    def start_project(self, project_id: str) -> None:
        self._reconcile_resources()
        if not self._ensure_project_lease(project_id):
            return
        status = self.lifecycle.status(project_id)
        if status is None:
            self._release_project_lease(project_id)
            return
        if status == "stopped":
            self.resume_project(project_id)
            return
        if status == "running":
            return
        if status in ("flag_found", "solved"):
            self._release_project_lease(project_id)
            return
        self._clear_project_member_retries(project_id)
        if not self.resources.acquire_task(project_id):
            self._queue_project(project_id)
            self._set_runtime_phase(project_id, "queued_capacity")
            self.state.logger.project(
                "task_admission_denied",
                project_id,
                active_tasks=self.state.limiter.active_tasks(),
                max_concurrent_tasks=self.state.limiter.max_concurrent_tasks,
            )
            return
        self._remove_queued_project(project_id)
        with suppress(LifecycleError):
            self.lifecycle.transition(project_id, "running")
        try:
            self._set_runtime_phase(project_id, "workspace")
            self.projects.ensure_dirs(project_id)
            self._set_runtime_phase(project_id, "challenge_environment")
            challenge_env = self.projects.start_challenge_env(project_id)
            if challenge_env is not None and not challenge_env.started:
                raise challenge_env.startup_error or SandboxStartupError(
                    challenge_env.error or "challenge environment failed to start",
                    operation="start challenge environment",
                )
            if self.lifecycle.status(project_id) != "running":
                self.resources.release_project(project_id)
                self.projects.teardown(project_id)
                self._release_project_lease(project_id)
                return
            self._set_runtime_phase(project_id, "sandbox_preflight")
            self.resources.preflight_project(project_id)
            self._set_runtime_phase(project_id, "assigning")
            assignment = self.diamond.assign_initial(project_id)
            if assignment is None:
                self.state.logger.project("no_members_available", project_id)
                self._mark_project_startup_failure(
                    project_id,
                    RuntimeError("no configured members available"),
                    event="project_start_failed",
                )
                return
            self.state.logger.project("project_scheduler_started", project_id, member=assignment.member, intent=assignment.intent_id)
            self._set_runtime_phase(project_id, "sandbox_starting")
            if self.lifecycle.status(project_id) != "running":
                self.resources.release_project(project_id)
                self.projects.teardown(project_id)
                self._release_project_lease(project_id)
                return
            launched = self._launch_member(
                project_id,
                assignment.member,
                assignment.intent_id,
                self._category(project_id),
                assignment.is_initial,
            )
            if not launched:
                raise RuntimeError(f"configured member is unavailable: {assignment.member}")
            if self.lifecycle.status(project_id) != "running":
                self.stop_project(project_id)
                return
            self._set_runtime_phase(project_id, "ready")
        except Exception as exc:
            self._mark_project_startup_failure(
                project_id,
                exc,
                event="project_start_failed",
            )
            raise

    def resume_project(self, project_id: str) -> None:
        if not self._ensure_project_lease(project_id):
            return
        self._reconcile_resources()
        status = self.lifecycle.status(project_id)
        if status is None:
            self._release_project_lease(project_id)
            return
        if status == "running":
            try:
                self.resources.preflight_project(project_id)
                self._dispatch_project(project_id)
            except Exception as exc:
                self._mark_project_startup_failure(
                    project_id,
                    exc,
                    event="project_resume_failed",
                )
                raise
            return
        if status != "stopped":
            self._release_project_lease(project_id)
            return
        self._clear_project_member_retries(project_id)
        if not self.resources.acquire_task(project_id):
            self._queue_project(project_id)
            self._set_runtime_phase(project_id, "queued_capacity")
            self.state.logger.project(
                "task_admission_denied",
                project_id,
                active_tasks=self.state.limiter.active_tasks(),
                max_concurrent_tasks=self.state.limiter.max_concurrent_tasks,
            )
            return
        self._remove_queued_project(project_id)
        try:
            try:
                self.lifecycle.transition(project_id, "running")
            except LifecycleError:
                with self.state.db.connect() as conn:
                    graph_store.set_status(conn, project_id, "running")
            self._set_runtime_phase(project_id, "workspace")
            self.projects.ensure_dirs(project_id)
            self._set_runtime_phase(project_id, "challenge_environment")
            challenge_env = self.projects.start_challenge_env(project_id)
            if challenge_env is not None and not challenge_env.started:
                raise challenge_env.startup_error or SandboxStartupError(
                    challenge_env.error or "challenge environment failed to start",
                    operation="start challenge environment",
                )
            if self.lifecycle.status(project_id) != "running":
                self.resources.release_project(project_id)
                self.projects.teardown(project_id)
                self._release_project_lease(project_id)
                return
            self._set_runtime_phase(project_id, "sandbox_preflight")
            self.resources.preflight_project(project_id)
            self._set_runtime_phase(project_id, "assigning")
            with self.state.db.connect() as conn:
                edge_store.expire_workers(
                    conn,
                    self.state.config.runtime.intent_timeout,
                    project_id,
                )
                detail = graph_store.project_detail(conn, project_id)
                if detail is None:
                    self.resources.release_project(project_id)
                    self.projects.teardown(project_id)
                    self._release_project_lease(project_id)
                    return
                graph_store.set_agent_state(conn, project_id, "diamond", "active")
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
            self._set_runtime_phase(project_id, "sandbox_starting")
            self._dispatch_project(project_id)
            self._set_runtime_phase(project_id, "ready")
        except Exception as exc:
            self._mark_project_startup_failure(
                project_id,
                exc,
                event="project_resume_failed",
            )
            raise

    # ---- reinforcements ----

    def handle_report(self, project_id: str, report) -> None:
        # Reports are now only a knowledge-sharing channel (bump to siblings).
        # Reinforcement decisions belong to the global monitor in ``_tick``.
        if self.lifecycle.status(project_id) != "running":
            return
        self._broadcast_bump(project_id, report)

    def _monitor_project(self, project_id: str) -> None:
        """Diamond's global watch: grade difficulty from blackboard evidence.

        Runs every scheduler tick for each running project.  Only a difficulty
        *escalation* triggers reinforcement; the first observation records the
        baseline, and de-escalation never kills running members.
        """
        with self.state.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            return
        with self._lock:
            struggle = sum(
                count
                for (pid, _), count in {**self._member_stall_counts, **self._member_failure_counts}.items()
                if pid == project_id
            )
        verdict = assess_project(detail, struggle_count=struggle)
        previous = self._monitor_levels.get(project_id)
        self._monitor_levels[project_id] = verdict.difficulty
        if previous != verdict.difficulty:
            self.state.logger.project(
                "diamond_monitor_assess",
                project_id,
                previous=previous,
                difficulty=verdict.difficulty,
                evidence=verdict.evidence,
            )
        if not escalated(previous, verdict.difficulty):
            return
        category = detail.project.category
        available_slots = max(
            0,
            self._max_running_for(category) - self._project_running_future_count(project_id),
        )
        self.state.logger.project(
            "diamond_monitor_escalate",
            project_id,
            difficulty=verdict.difficulty,
            evidence=verdict.evidence,
            available_slots=available_slots,
        )
        self.diamond.reinforce_from_monitor(
            project_id,
            verdict,
            category=category,
            available_slots=available_slots,
        )

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
            )

    def _format_report_bump(self, report) -> str:
        parts = [
            f"{report.member} shared a progress report.",
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

    def _broadcast_fact(self, project_id: str, fact_id: str, source_member: str | None) -> None:
        with self.state.db.connect() as conn:
            facts = {fact.id: fact.description for fact in node_store.list_facts(conn, project_id)}
        description = facts.get(fact_id)
        if not description:
            return
        insight = (
            f"Confirmed fact {fact_id}"
            + (f" from {source_member}" if source_member else "")
            + f": {description}\n\n"
            "Treat this as shared graph state. Anchor follow-up work to this fact when relevant, "
            "and avoid re-running steps already covered by it."
        )
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        bumped: list[str] = []
        for member in members:
            if source_member and member.name == source_member:
                continue
            member.bump(insight)
            bumped.append(member.name)
        if bumped:
            self.state.logger.project(
                "member_fact_broadcast",
                project_id,
                source=source_member,
                fact=fact_id,
                targets=bumped,
            )

    # ---- member execution ----

    def _member_config(self, name: str):
        for m in self.state.config.members:
            if m.name == name:
                return m
        return None

    def _launch_member(self, project_id, member_name, intent_id, category, is_initial) -> bool:
        if self._stop.is_set():
            return False
        cfg = self._member_config(member_name)
        if cfg is None:
            return False
        assignment = self._record_member_assignment(project_id, member_name, intent_id)
        if assignment is None:
            return False
        lease_owner, lease_token = assignment
        with self._lock:
            self._intent_leases[(project_id, intent_id)] = IntentLease(
                worker=member_name,
                owner=lease_owner,
                token=lease_token,
            )
        member = None
        try:
            sandbox = self.resources.sandbox_for(project_id, member_name)
            # Crypto members run without a step budget: 0 means unlimited.
            # Loop-break / invalid-action guards and the intent lease still
            # apply, so a spinning model is still stopped deterministically.
            unlimited = category == "crypto"
            deps = MemberDeps(
                db=self.state.db,
                logger=self.state.logger,
                sandbox=sandbox,
                mcps=self.state.mcps,
                registry=self.state.registry,
                memory=self.state.memory,
                container_mcps=self._container_mcps(project_id, member_name),
                max_steps=0 if unlimited else self.state.config.runtime.max_member_steps,
                max_actions_per_task=(
                    0 if unlimited else self.state.config.runtime.max_member_actions_per_task
                ),
                on_report=self.handle_report,
                on_flag=self.on_flag_found,
                lease_owner=lease_owner,
                lease_token=lease_token,
            )
            script = self.scripts.get((project_id, member_name)) or self.scripts.get(member_name)
            member = create_member(cfg, deps, script=script)
            with self._lock:
                stopping = self._stop.is_set()
                if not stopping:
                    self._members.setdefault(project_id, {})[member_name] = member
            if stopping:
                member.stop()
                self._release_member_assignment(
                    project_id,
                    intent_id,
                    lease_owner,
                    lease_token,
                )
                return False
            future = self.executor.submit(
                self._run_member,
                project_id,
                member,
                intent_id,
                category,
                is_initial,
            )
        except Exception:
            if member is not None:
                member.stop()
                with self._lock:
                    if self._members.get(project_id, {}).get(member_name) is member:
                        self._members[project_id].pop(member_name, None)
            self._release_member_assignment(
                project_id,
                intent_id,
                lease_owner,
                lease_token,
            )
            raise
        with self._lock:
            self._futures.setdefault(project_id, []).append(future)
            self._task_index[(project_id, intent_id)] = future
        return True

    def _container_mcps(self, project_id: str, member: str) -> dict[str, MCPRegistryTarget]:
        if self.state.pool.backend != "docker":
            return {}
        container = task_container_name(project_id)
        workdir = member_workdir(member)

        def target(server: str, *, env: dict[str, str] | None = None) -> MCPClient:
            args = ["exec", "-i"]
            for key, value in (env or {}).items():
                args.extend(["-e", f"{key}={value}"])
            args.extend(
                [
                    "-w",
                    workdir,
                    container,
                    "python3",
                    "-m",
                    "backend.mcp.mcp_server",
                    server,
                ]
            )
            return MCPClient.stdio("docker", args, read_timeout=600)

        runtime = self.state.config.runtime
        browser_env = {
            "IPC_BROWSER_PROJECT_ID": project_id,
            "IPC_BROWSER_MEMBER": member,
            "IPC_BROWSER_WORKDIR": workdir,
            "IPC_BROWSER_SHARED_DIR": "/workspace/shared",
            "IPC_BROWSER_ARTIFACT_ROOT": f"{workdir}/browser-artifacts",
            "IPC_BROWSER_EVENT_LIMIT": str(runtime.browser_event_limit),
            "IPC_BROWSER_CONSOLE_LIMIT": str(runtime.browser_console_limit),
            "IPC_BROWSER_ERROR_LIMIT": str(runtime.browser_error_limit),
            "IPC_BROWSER_RESPONSE_PREVIEW_BYTES": str(runtime.browser_response_preview_bytes),
            "IPC_BROWSER_ALLOWED_ORIGINS": json.dumps(runtime.browser_allowed_origins),
            "IPC_BROWSER_ARTIFACT_MAX_BYTES": str(runtime.browser_artifact_max_bytes),
        }
        targets = {
            "browser": target("browser", env=browser_env),
            "reverse": target("reverse"),
        }
        if self.state.config.runtime.zap_enabled:
            targets["zap"] = target(
                "zap", env={"ZAP_API_URL": "http://ipc-zap:8080"}
            )
        return targets

    def _record_member_assignment(
        self, project_id: str, member_name: str, intent_id: str
    ) -> tuple[str, str] | None:
        lease_owner = (
            f"{self.state.instance_id}:{member_name}:{intent_id}:{uuid.uuid4().hex[:10]}"
        )
        with self.state.db.connect() as conn:
            lease_token = edge_store.claim_intent(
                conn,
                project_id,
                intent_id,
                member_name,
                lease_owner=lease_owner,
                timeout=self.state.config.runtime.intent_timeout,
            )
            if lease_token is None:
                return None
            rows = conn.execute(
                "SELECT fact_id FROM intent_sources WHERE intent_id = %s AND project_id = %s ORDER BY fact_id",
                (intent_id, project_id),
            ).fetchall()
            start_fact_id = "origin"
            for row in rows:
                if node_store.fact_exists(conn, project_id, row["fact_id"]):
                    start_fact_id = row["fact_id"]
                    break
            graph_store.add_agent(
                conn,
                project_id,
                member_name,
                "member",
                state="active",
                start_fact_id=start_fact_id,
            )
            conn.execute(
                "UPDATE agents SET state = 'active', start_fact_id = %s "
                "WHERE project_id = %s AND name = %s",
                (start_fact_id, project_id, member_name),
            )
            if not graph_store.link_exists(conn, project_id, "diamond", member_name, "assign"):
                graph_store.add_link(conn, project_id, "diamond", member_name, "assign")
            if not graph_store.link_exists(conn, project_id, member_name, f"intent:{intent_id}", "explore"):
                graph_store.add_link(conn, project_id, member_name, f"intent:{intent_id}", "explore")
        return lease_owner, lease_token

    def _release_member_assignment(
        self,
        project_id: str,
        intent_id: str,
        lease_owner: str,
        lease_token: str,
    ) -> None:
        with self._lock:
            current = self._intent_leases.get((project_id, intent_id))
            if current is not None and current.token == lease_token:
                self._intent_leases.pop((project_id, intent_id), None)
        with self.state.db.connect() as connection:
            edge_store.release_intent(
                connection,
                project_id,
                intent_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
            )

    def _run_member(self, project_id, member, intent_id, category, is_initial):
        try:
            return asyncio.run(
                member.solve_async(project_id, intent_id, category, is_initial=is_initial)
            )
        except Exception as exc:
            self.state.logger.project("member_crash", project_id, member=member.name, error=str(exc))
        finally:
            with self._lock:
                self._members.get(project_id, {}).pop(member.name, None)
            with self.state.db.connect() as conn:
                row = graph_store.get_project_row(conn, project_id)
                if row is not None and row["status"] == "running":
                    graph_store.set_agent_state(conn, project_id, member.name, "idle")

    # ---- stop ----

    def stop_project(self, project_id: str) -> None:
        self._remove_queued_project(project_id)
        self._clear_project_member_retries(project_id)
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for m in members:
            m.stop()
        self._release_owned_intent_leases(project_id)
        with self.state.db.connect() as conn:
            conn.execute(
                "UPDATE agents SET state = 'idle' WHERE project_id = %s AND role = 'member'",
                (project_id,),
            )
        self.resources.release_project(project_id)
        self.projects.teardown(project_id)
        phase = "solved" if self.lifecycle.status(project_id) == "solved" else "stopped"
        self._set_runtime_phase(project_id, phase)
        self._release_project_lease(project_id)

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
        # Stop competing solvers as soon as a candidate reaches verification.
        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for member in members:
            member.stop()

        if self.lifecycle.status(project_id) == "running":
            with suppress(LifecycleError):
                self.lifecycle.transition(project_id, "flag_found")

        verdict = verify_flag(state.db, project_id)
        if not verdict["ok"]:
            state.logger.project("ipc_verification_failed", project_id, reasons=verdict["reasons"])
            with state.db.connect() as connection:
                connection.execute(
                    "UPDATE projects SET status = 'failed', terminal_reason = %s, updated_at = now() "
                    "WHERE id = %s AND status <> 'solved'",
                    ("; ".join(verdict["reasons"]), project_id),
                )
            self.stop_project(project_id)
            return

        with state.db.connect() as connection:
            solved = accept_verified_flag(
                connection,
                project_id,
                verdict["flag"],
                source="orchestrator",
            )
            enqueue_postprocess(connection, project_id)
            row = graph_store.get_project_row(connection, project_id)
            already_broadcast = connection.execute(
                "SELECT 1 FROM broadcasts WHERE project_id = %s LIMIT 1", (project_id,)
            ).fetchone()
            if row is not None and already_broadcast is None:
                graph_store.add_broadcast(
                    connection, project_id, row["title"], row["flag"] or ""
                )

        state.logger.project("solved", project_id, flag=solved["flag"])
        self._set_runtime_phase(project_id, "solved")
        self.stop_project(project_id)
        self._dispatch_postprocess()

    def _dispatch_postprocess(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._postprocess_future is not None and not self._postprocess_future.done():
                return
            self._postprocess_future = self.executor.submit(self._drain_postprocess_jobs)

    def _drain_postprocess_jobs(self) -> None:
        while not self._stop.is_set():
            with self.state.db.connect() as connection:
                recover_expired_jobs(connection)
                job = claim_next_job(
                    connection,
                    self.state.instance_id,
                    lease_seconds=self._POSTPROCESS_LEASE_SECONDS,
                )
            if job is None:
                return
            heartbeat_stop = threading.Event()
            lease_lost = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_postprocess_job,
                args=(job, heartbeat_stop, lease_lost),
                name=f"ipc-postprocess-heartbeat-{job.id}",
                daemon=True,
            )
            heartbeat.start()
            try:
                prepared = self._prepare_postprocess_job(job)
            except Exception as exc:
                heartbeat_stop.set()
                heartbeat.join()
                error = f"{type(exc).__name__}: {exc}"
                with self.state.db.connect() as connection:
                    failed = fail_job(connection, job, error)
                if failed:
                    self.state.logger.project(
                        f"{job.kind}_postprocess_failed",
                        job.project_id,
                        attempt=job.attempts,
                        error=error,
                    )
                else:
                    self._log_stale_postprocess_job(job, stage="failure")
                continue

            heartbeat_stop.set()
            heartbeat.join()
            if lease_lost.is_set():
                self._log_stale_postprocess_job(job, stage="prepare")
                continue

            try:
                row = self._commit_postprocess_job(job, prepared)
            except PostprocessLeaseLost:
                self._log_stale_postprocess_job(job, stage="commit")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self.state.db.connect() as connection:
                    failed = fail_job(connection, job, error)
                if failed:
                    self.state.logger.project(
                        f"{job.kind}_postprocess_failed",
                        job.project_id,
                        attempt=job.attempts,
                        error=error,
                    )
                else:
                    self._log_stale_postprocess_job(job, stage="commit_failure")
            else:
                self.state.logger.project(
                    f"{job.kind}_postprocess_completed",
                    job.project_id,
                    attempt=job.attempts,
                )
                if row is not None and row["postprocess_status"] == "completed":
                    self.diamond.draw_completion(job.project_id)

    def _heartbeat_postprocess_job(self, job, stop: threading.Event, lost: threading.Event) -> None:
        interval = max(1, self._POSTPROCESS_LEASE_SECONDS // 3)
        while not stop.wait(interval):
            try:
                with self.state.db.connect() as connection:
                    renewed = renew_job(
                        connection,
                        job,
                        lease_seconds=self._POSTPROCESS_LEASE_SECONDS,
                    )
            except Exception as exc:
                self.state.logger.project(
                    "postprocess_heartbeat_error",
                    job.project_id,
                    kind=job.kind,
                    job_id=job.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not renewed:
                lost.set()
                return

    def _prepare_postprocess_job(self, job):
        if job.kind == "writeup":
            return self.diamond.generate_wp_content(job.project_id)
        if job.kind in ("memory", "archive"):
            return None
        raise ValueError(f"unknown postprocess job kind: {job.kind}")

    def _commit_postprocess_job(self, job, prepared):
        rollback_file = None
        written_memories = []
        try:
            with self.state.db.connect() as connection:
                if not lock_job(
                    connection,
                    job,
                    lease_seconds=self._POSTPROCESS_LEASE_SECONDS,
                ):
                    raise PostprocessLeaseLost(
                        f"postprocess lease lost for job {job.id}"
                    )

                if job.kind == "writeup":
                    content, expected_flag = prepared
                    path, rollback_file = persist_validated_writeup(
                        connection,
                        job.project_id,
                        self.state.wp_dir,
                        content,
                        expected_flag=expected_flag,
                    )
                elif job.kind == "memory":
                    written = write_memory(
                        self.state.db,
                        job.project_id,
                        self.state.memory,
                        connection=connection,
                        mirror=False,
                    )
                    written_memories = written
                elif job.kind == "archive":
                    archive = archive_completed_project(
                        self.state,
                        job.project_id,
                        connection=connection,
                    )
                else:
                    raise ValueError(f"unknown postprocess job kind: {job.kind}")

                if not complete_job(connection, job, lease_locked=True):
                    raise PostprocessLeaseLost(
                        f"postprocess lease expired while committing job {job.id}"
                    )
                row = graph_store.get_project_row(connection, job.project_id)
        except Exception:
            if rollback_file is not None:
                rollback_file()
            raise
        if written_memories and self.state.memory.export_dir is not None:
            for memory in written_memories:
                try:
                    self.state.memory._mirror_to_disk(memory)
                except Exception as exc:
                    self.state.logger.project(
                        "memory_mirror_failed",
                        job.project_id,
                        memory=memory.id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        if job.kind == "writeup":
            self.state.logger.project("wp_written", job.project_id, path=path)
        elif job.kind == "memory":
            self.state.logger.memory(
                "experience_written", job.project_id, count=len(written_memories)
            )
        elif job.kind == "archive":
            self.state.logger.project(
                "outputs_archived",
                job.project_id,
                wp_filename=archive["wp_filename"],
                log_filename=archive["log_filename"],
            )
        return row

    def _log_stale_postprocess_job(self, job, *, stage: str) -> None:
        self.state.logger.project(
            "postprocess_result_discarded",
            job.project_id,
            kind=job.kind,
            job_id=job.id,
            attempt=job.attempts,
            stage=stage,
        )

    # ---- scheduler loop ----

    def _run_loop(self) -> None:
        interval = max(1, self.state.config.runtime.interval)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                # The fallback handler itself must never kill the loop: it
                # logs through the DB-backed resolver, which is exactly what
                # fails when the tick died on a database error.
                try:
                    self.state.logger.project("scheduler_error", "system", error=str(exc))
                except Exception:
                    print(f"[ipc-orchestrator] tick failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._stop.wait(interval)

    def _tick(self) -> None:
        self._heartbeat_project_leases()
        self._heartbeat_intent_leases()
        self._reconcile_resources()
        self._drain_pending_projects()
        with self.state.db.connect() as conn:
            graph_store.expire_reason_leases(conn, self.state.config.runtime.reason_timeout)
            edge_store.expire_workers(conn, self.state.config.runtime.intent_timeout)
            recover_expired_jobs(conn)
        self._dispatch_postprocess()
        self._reap_finished_futures()
        with self.state.db.connect() as conn:
            summaries = graph_store.project_summaries(conn)
        self._initialize_reason_checkpoints(summaries)
        for summary in summaries:
            if summary.status == "flag_found":
                # ``flag_found`` is a durable commit-in-progress state. If an
                # instance died between recording evidence and atomically
                # accepting it, the lease winner resumes verification here.
                if self._ensure_project_lease(summary.id):
                    self.state.logger.project(
                        "flag_verification_recovered",
                        summary.id,
                        instance=self.state.instance_id,
                    )
                    self.on_flag_found(summary.id)
                continue
            if summary.status != "running":
                continue
            with self._lock:
                owned_before = summary.id in self._project_leases
            if not self._ensure_project_lease(summary.id):
                continue
            if not owned_before:
                self._schedule_running_recovery(summary.id)
                continue
            # ``start_project`` marks the project running before slow Docker
            # setup so the UI can expose a Stop control.  Do not dispatch a
            # second Member until that startup worker reaches ``ready``.
            if self._startup_in_progress(summary.id):
                continue
            self._monitor_project(summary.id)
            if not self.resources.acquire_task(summary.id):
                self._queue_project(summary.id)
                continue
            try:
                self._dispatch_project(summary.id)
            except Exception as exc:
                self._mark_project_startup_failure(
                    summary.id,
                    exc,
                    event="project_dispatch_failed",
                )

    def _schedule_running_recovery(self, project_id: str) -> None:
        with self._lock:
            if project_id in self._startup_project_ids:
                return
            self._startup_project_ids.add(project_id)
        try:
            future = self.startup_executor.submit(self._recover_running_project, project_id)
        except Exception as exc:
            with self._lock:
                self._startup_project_ids.discard(project_id)
            self._mark_project_startup_failure(
                project_id,
                SandboxStartupError(
                    f"startup executor rejected recovery: {exc}",
                    operation="schedule project recovery",
                ),
                event="project_recovery_schedule_failed",
            )
            return
        with self._lock:
            self._startup_futures[project_id] = future

    def _recover_running_project(self, project_id: str) -> None:
        try:
            if not self._ensure_project_lease(project_id):
                return
            if not self.resources.acquire_task(project_id):
                self._queue_project(project_id)
                self._set_runtime_phase(project_id, "queued_capacity")
                self._release_project_lease(project_id)
                return
            self._set_runtime_phase(project_id, "recovering_workspace")
            self.projects.ensure_dirs(project_id)
            challenge_env = self.projects.start_challenge_env(project_id)
            if challenge_env is not None and not challenge_env.started:
                raise challenge_env.startup_error or SandboxStartupError(
                    challenge_env.error or "challenge environment recovery failed",
                    operation="recover challenge environment",
                )
            with self.state.db.connect() as connection:
                edge_store.expire_workers(
                    connection,
                    self.state.config.runtime.intent_timeout,
                    project_id,
                )
            self._set_runtime_phase(project_id, "recovering_preflight")
            self.resources.preflight_project(project_id)
            self._set_runtime_phase(project_id, "recovering_dispatch")
            self._dispatch_project(project_id)
            self._set_runtime_phase(project_id, "ready")
            self.state.logger.project(
                "project_recovered", project_id, instance=self.state.instance_id
            )
        except Exception as exc:
            self._mark_project_startup_failure(
                project_id,
                exc,
                event="project_recovery_failed",
            )
        finally:
            with self._lock:
                self._startup_project_ids.discard(project_id)
                self._startup_futures.pop(project_id, None)

    def _dispatch_project(self, project_id: str) -> None:
        category = self._category(project_id)
        with self._lock:
            project_tasks = {k: f for k, f in self._task_index.items() if k[0] == project_id and not f.done()}
            active_members = set(self._members.get(project_id, {}).keys())
        max_running = self._max_running_for(category)
        available_slots = max_running - len(project_tasks)
        if available_slots <= 0:
            return
        with self.state.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            return
        running = {intent_id for (pid, intent_id), fut in project_tasks.items() if pid == project_id and not fut.done()}
        open_intents = [i for i in detail.intents if i.to is None]
        all_claimable = [i for i in open_intents if i.id not in running]
        claimable = [
            intent for intent in all_claimable
            if not self._member_retry_blocked(project_id, intent.id)
        ]
        if not claimable and all_claimable:
            return
        claimed = [
            i for i in claimable
            if i.worker is not None and i.worker not in active_members
        ]
        unclaimed = [i for i in claimable if i.worker is None]
        if not claimed and not unclaimed:
            reason_trigger = self._reason_trigger(detail)
            if reason_trigger is None:
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
            if available_slots <= 0:
                break
            if intent.id in running:
                continue
            if intent.worker is not None and intent.worker in active_members:
                continue
            member_name = self._select_member_for_intent(project_id, detail, intent, active_members)
            if member_name is None:
                continue
            launched = self._launch_member(
                project_id,
                member_name,
                intent.id,
                category,
                intent.description.startswith("bootstrap"),
            )
            if launched is False:
                continue
            running.add(intent.id)
            active_members.add(member_name)
            available_slots -= 1

    def _member_retry_blocked(self, project_id: str, intent_id: str) -> bool:
        with self._lock:
            retry_at = self._member_retry_not_before.get((project_id, intent_id))
        return retry_at is not None and monotonic() < retry_at

    def _schedule_member_retry(
        self,
        project_id: str,
        intent_id: str,
        *,
        error: str,
    ) -> None:
        if self.lifecycle.status(project_id) != "running":
            return
        key = (project_id, intent_id)
        with self._lock:
            count = self._member_failure_counts.get(key, 0) + 1
            self._member_failure_counts[key] = count
            delay = self._MEMBER_RETRY_DELAYS[min(count - 1, len(self._MEMBER_RETRY_DELAYS) - 1)]
            self._member_retry_not_before[key] = monotonic() + delay
        safe_error = error[:1000]
        self.state.logger.project(
            "member_task_retry_scheduled",
            project_id,
            intent=intent_id,
            attempt=count,
            delay_seconds=delay,
            error=safe_error,
        )
        self._set_runtime_phase(
            project_id,
            "degraded",
            error=(
                f"Member action failed for {intent_id}; retry {count} in {delay}s. "
                f"{safe_error}"
            )[:1500],
        )

    def _record_member_stall(self, project_id: str, intent_id: str) -> tuple[int, bool]:
        """Defer a repeatedly stalled intent so a concrete alternate branch can run."""
        key = (project_id, intent_id)
        with self._lock:
            count = self._member_stall_counts.get(key, 0) + 1
            self._member_stall_counts[key] = count
            should_defer = count >= self._MAX_CONSECUTIVE_STALLS
            if should_defer:
                self._member_retry_not_before[key] = monotonic() + self._STALL_DEFER_SECONDS
        if should_defer:
            # The finished-future path has already released the intent claim;
            # only the redispatch backoff is needed here.
            self.state.logger.project(
                "member_intent_deferred",
                project_id,
                intent=intent_id,
                stalls=count,
                delay_seconds=self._STALL_DEFER_SECONDS,
                reason="repeated_stalls",
            )
        return count, should_defer

    def _clear_member_stalls(self, project_id: str, intent_id: str) -> None:
        with self._lock:
            self._member_stall_counts.pop((project_id, intent_id), None)

    def _clear_member_retry(self, project_id: str, intent_id: str) -> None:
        key = (project_id, intent_id)
        with self._lock:
            existed = key in self._member_failure_counts or key in self._member_retry_not_before
            self._member_failure_counts.pop(key, None)
            self._member_retry_not_before.pop(key, None)
            project_has_failures = any(pid == project_id for pid, _ in self._member_failure_counts)
        if existed and not project_has_failures and self.lifecycle.status(project_id) == "running":
            self._set_runtime_phase(project_id, "ready")

    def _clear_project_member_retries(self, project_id: str) -> None:
        with self._lock:
            for key in [key for key in self._member_failure_counts if key[0] == project_id]:
                self._member_failure_counts.pop(key, None)
            for key in [key for key in self._member_retry_not_before if key[0] == project_id]:
                self._member_retry_not_before.pop(key, None)
            for key in [key for key in self._member_stall_counts if key[0] == project_id]:
                self._member_stall_counts.pop(key, None)

    def _reconcile_resources(self) -> None:
        with self.state.db.connect() as conn:
            summaries = graph_store.project_summaries(conn)
        active_project_ids = {
            summary.id
            for summary in summaries
            if summary.status in ("running", "flag_found")
        }
        reclaimed = self.resources.reclaim_orphaned_projects(active_project_ids)
        for project_id in self.state.limiter.active_tasks():
            if project_id not in active_project_ids:
                self.state.limiter.release(project_id)
        if reclaimed:
            self.state.logger.project(
                "orphaned_project_resources_reclaimed",
                "system",
                projects=reclaimed,
            )

    def _queue_project(self, project_id: str) -> None:
        with self._lock:
            if project_id in self._pending_project_ids:
                return
            self._pending_project_ids.add(project_id)
            self._pending_projects.append(project_id)

    def _remove_queued_project(self, project_id: str) -> None:
        with self._lock:
            if project_id not in self._pending_project_ids:
                return
            self._pending_project_ids.discard(project_id)
            self._pending_projects = deque(
                queued for queued in self._pending_projects if queued != project_id
            )

    def _drain_pending_projects(self) -> None:
        while True:
            with self._lock:
                if not self._pending_projects:
                    return
                project_id = self._pending_projects[0]
            if not self.resources.can_admit_task(project_id):
                return
            self._remove_queued_project(project_id)
            status = self.lifecycle.status(project_id)
            if status == "created":
                self.start_project_async(project_id)
            elif status == "stopped":
                self.start_project_async(project_id)
            elif status == "running":
                if (
                    not self._startup_in_progress(project_id)
                    and self._ensure_project_lease(project_id)
                ):
                    self._schedule_running_recovery(project_id)

    def _select_member_for_intent(self, project_id: str, detail, intent, active: set[str]) -> str | None:
        preferred = self._intent_member_candidates(detail, intent)
        return self._pick_idle_member(project_id, detail=detail, active=active, preferred=preferred)

    def _intent_member_candidates(self, detail, intent) -> list[str]:
        roles = {agent.name: agent.role for agent in detail.agents}
        candidates: list[str] = []

        def add(name: str | None) -> None:
            if name and roles.get(name) == "member" and name not in candidates:
                candidates.append(name)

        add(intent.worker)
        add(intent.creator)
        intent_ref = f"intent:{intent.id}"
        for link in detail.agent_links:
            if link.kind == "explore" and link.dst == intent_ref:
                add(link.src)
        return candidates

    def _pick_idle_member(
        self,
        project_id: str,
        detail=None,
        active: set[str] | None = None,
        preferred: list[str] | None = None,
    ) -> str | None:
        if active is None:
            with self._lock:
                active = set(self._members.get(project_id, {}).keys())
        idle = [member.name for member in self.state.config.available_members() if member.name not in active]
        if not idle:
            return None
        preferred = preferred or []
        for name in preferred:
            if name in idle:
                return name
        if detail is None:
            with self.state.db.connect() as conn:
                detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            return idle[0]
        project_members = {agent.name for agent in detail.agents if agent.role == "member"}
        candidates = [name for name in idle if name in project_members] or idle
        config_order = {member.name: idx for idx, member in enumerate(self.state.config.available_members())}
        return min(candidates, key=lambda name: self._member_dispatch_score(detail, name, config_order.get(name, 10_000)))

    def _member_dispatch_score(self, detail, name: str, config_index: int) -> tuple[int, int, int]:
        load = 0
        last_link_id = 0
        for intent in detail.intents:
            if intent.worker == name:
                load += 2
            if intent.creator == name:
                load += 1
        for link in detail.agent_links:
            if link.kind == "explore" and link.src == name:
                load += 1
                last_link_id = max(last_link_id, link.id)
            elif link.kind == "assign" and link.dst == name:
                last_link_id = max(last_link_id, link.id)
        return (load, last_link_id, config_index)

    def _project_running_future_count(self, project_id: str) -> int:
        with self._lock:
            return sum(
                1
                for (pid, _), future in self._task_index.items()
                if pid == project_id and not future.done()
            )

    def _project_open_intent_count(self, detail) -> int:
        return sum(1 for intent in detail.intents if intent.to is None)

    def _initialize_reason_checkpoints(self, summaries) -> None:
        running_ids = {summary.id for summary in summaries if summary.status == "running"}
        for project_id in list(self._reason_checkpoints):
            if project_id not in running_ids:
                self._reason_checkpoints.pop(project_id, None)
        for project_id in list(self._monitor_levels):
            if project_id not in running_ids:
                self._monitor_levels.pop(project_id, None)
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
            crashed = False
            failure_error = "member task returned no result"
            try:
                result = future.result()
            except Exception as exc:
                crashed = True
                failure_error = f"{type(exc).__name__}: {exc}"
                self.state.logger.project(
                    "member_task_crash",
                    project_id,
                    intent=intent_id,
                    error=failure_error,
                )
                result = None
            finally:
                with self._lock:
                    if self._task_index.get((project_id, intent_id)) is future:
                        self._task_index.pop((project_id, intent_id), None)
                    lease = self._intent_leases.get((project_id, intent_id))
                if lease is not None:
                    self._release_member_assignment(
                        project_id,
                        intent_id,
                        lease.owner,
                        lease.token,
                    )
            if result is None:
                if not crashed:
                    self.state.logger.project("member_task_failed", project_id, intent=intent_id)
                self._schedule_member_retry(
                    project_id,
                    intent_id,
                    error=failure_error,
                )
                continue
            if result.status == "failed":
                self.state.logger.project(
                    "member_task_failed",
                    project_id,
                    intent=intent_id,
                    steps=result.steps,
                    error=result.error,
                    retryable=result.retryable is not False,
                    error_kind=result.error_kind,
                )
                if result.retryable is False:
                    # A terminal validation/configuration result must also
                    # invalidate any backoff left by an earlier transient
                    # failure for the same intent.
                    self._clear_member_retry(project_id, intent_id)
                else:
                    self._schedule_member_retry(
                        project_id,
                        intent_id,
                        error=result.error or "member action failed",
                    )
                continue
            self._clear_member_retry(project_id, intent_id)
            if result.status == "stalled":
                stalls, deferred = self._record_member_stall(project_id, intent_id)
                self.state.logger.project(
                    "member_task_stalled",
                    project_id,
                    intent=intent_id,
                    steps=result.steps,
                    retryable=result.retryable,
                    error_kind=result.error_kind,
                    error=result.error,
                    consecutive_stalls=stalls,
                    deferred=deferred,
                )
            elif result.status == "done":
                self._clear_member_stalls(project_id, intent_id)
                self.state.logger.project("member_task_done", project_id, intent=intent_id, steps=result.steps)
            elif result.status == "concluded":
                self._clear_member_stalls(project_id, intent_id)
                self.state.logger.project("member_task_concluded", project_id, intent=intent_id, fact=result.fact_id)
                if result.fact_id is not None:
                    self._broadcast_fact(project_id, result.fact_id, self._intent_worker(project_id, intent_id))
            elif result.status == "flag":
                self._clear_member_stalls(project_id, intent_id)
                self.state.logger.project("member_task_flag", project_id, intent=intent_id, flag=result.flag)

    def _intent_worker(self, project_id: str, intent_id: str) -> str | None:
        with self.state.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            return row["worker"] if row is not None else None

    # ---- test helper ----

    def wait(self, project_id: str, timeout: float = 30.0) -> None:
        from time import monotonic, sleep

        deadline = monotonic() + timeout
        while monotonic() < deadline:
            with self._lock:
                futures = list(self._futures.get(project_id, []))
                startup = self._startup_futures.get(project_id)
            if startup is not None:
                futures.append(startup)
            pending = [f for f in futures if not f.done()]
            status = self.lifecycle.status(project_id)
            if (
                not pending
                and status in ("solved", "stopped", "failed", "infra_error", "flag_found")
                and (status == "solved" or self._completing == set())
            ):
                return
            sleep(0.05)
