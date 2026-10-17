from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from psycopg.errors import UndefinedColumn, UndefinedTable
from psycopg.types.json import Jsonb

from backend.blackboard import edge_store, graph_store, node_store
from backend.competition.transport import Envelope
from backend.core.archive import archive_completed_project
from backend.core.diamond import Diamond
from backend.core.engine_lease import (
    claim_engine_lease,
    release_engine_lease,
)
from backend.core.ipc import (
    accept_verified_flag,
    lock_project_completion,
    requires_platform_verdict,
    verify_flag,
)
from backend.core.lifecycle import Lifecycle, LifecycleError
from backend.core.memory_writer import write_memory
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
from backend.core.wp_writer import persist_validated_writeup
from backend.mcp.mcp_client import MCPClient, MCPRegistryTarget
from backend.members.base_member import MemberDeps
from backend.members.factory import create_member
from backend.platform.ret2shell import (
    Ret2ShellError,
    Ret2ShellPreflightError,
    Ret2ShellRateLimitError,
)
from backend.sandbox.errors import SandboxStartupError
from backend.sandbox.task_sandbox import member_workdir, task_container_name

_recovery_logger = logging.getLogger(__name__)

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
    _VERDICT_MAX_UNKNOWN_ATTEMPTS = 3

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
        self._reserved_members: set[str] = set()
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
        self._engine_leases: dict[str, tuple[str, str, int]] = {}
        self._engine_owner = f"{state.instance_id}:legacy"
        self._intent_leases: dict[tuple[str, str], IntentLease] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._postprocess_future: Any | None = None
        self._verdict_unknown_attempts: dict[str, int] = {}
        self._verdict_deferred_logged: set[str] = set()
        self._legacy_block_counts: dict[tuple[str, str, str], int] = {}
        self._legacy_block_next_log: dict[tuple[str, str, str], float] = {}
        self._legacy_block_emitted_counts: dict[tuple[str, str, str], int] = {}
        self._throttle_counts: dict[tuple[str, str], int] = {}
        self._throttle_next_log: dict[tuple[str, str], float] = {}
        self._throttle_emitted: dict[tuple[str, str], int] = {}
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

        self.startup_executor.shutdown(wait=True, cancel_futures=True)
        self._stop_members()
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            engine_projects = list(getattr(self, "_engine_leases", {}))
        for project_id in engine_projects:
            self._release_project_engine_lease(project_id)

    def _stop_members(self) -> None:
        with self._lock:
            members = [m for proj in self._members.values() for m in proj.values()]
        for m in members:
            m.stop()


    def _category(self, project_id: str) -> str:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            return row["category"] if row else "misc"


    def start_project_async(self, project_id: str) -> dict[str, str | None]:
        """Queue slow Docker/bootstrap work without blocking an HTTP or MCP call."""

        if self._stop.is_set():
            raise RuntimeError("orchestrator is shutting down")

        if self._competition_project(project_id) and not self._competition_running(project_id):
            return self.runtime_status(project_id)

        status = self.lifecycle.status(project_id)
        if status is None:
            raise ValueError(f"project not found: {project_id}")
        if status in ("flag_found", "solved"):
            return self.runtime_status(project_id)
        if status == "running" and not self._startup_in_progress(project_id):
            with self._lock:
                already_owned = project_id in self._project_leases
            if already_owned:
                self._submit_redispatch(project_id)
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
            _recovery_logger.debug('Error handled in _run_project_startup', exc_info=True)
            self._mark_project_startup_failure(
                project_id,
                exc,
                event="project_background_start_failed",
            )
        finally:
            with self._lock:
                self._startup_project_ids.discard(project_id)
                self._startup_futures.pop(project_id, None)

    def _engine_context(self, project_id: str) -> tuple[str, str] | None:
        """Select the scheduler identity that may own a Project.

        Competition projects are executed by the legacy Member runtime for
        compatibility, but the competition coordinator remains the scheduler
        owner.  Reusing its owner token lets both components renew one fence
        without allowing a standalone legacy start to steal it.
        """
        if self._competition_project(project_id):
            owner = getattr(self.state, "competition_engine_owner", None)
            if not owner:
                return None
            return "competition", str(owner)
        return "legacy", self._engine_owner

    def _claim_project_engine_lease(self, project_id: str) -> bool:
        context = self._engine_context(project_id)
        if context is None:
            return False
        kind, owner = context
        try:
            lease = claim_engine_lease(
                self.state.db, project_id, kind, owner,
                seconds=max(30, self.state.config.runtime.interval * 6),
            )
        except (UndefinedColumn, UndefinedTable):
            return True
        if lease is None:
            self._throttled_log(
                "engine_lease_conflict", project_id, project_id,
                requested_kind=kind, requested_owner=owner,
            )
            return False
        with self._lock:
            if not hasattr(self, "_engine_leases"):
                self._engine_leases = {}
            self._engine_leases[project_id] = (
                kind, owner, int(lease["engine_epoch"])
            )
        return True

    def _release_project_engine_lease(self, project_id: str) -> None:
        with self._lock:
            context = getattr(self, "_engine_leases", {}).pop(project_id, None)
        if context is None:
            return
        try:
            release_engine_lease(
                self.state.db, project_id, context[0], context[1], epoch=context[2]
            )
        except Exception:
            _recovery_logger.debug('Error handled in _release_project_engine_lease', exc_info=True)
            return

    def _ensure_project_lease(self, project_id: str) -> bool:
        if not self._claim_project_engine_lease(project_id):
            with self._lock:
                had_project_lease = project_id in self._project_leases
            if had_project_lease:
                self._release_project_lease(project_id)
            return False
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
            self._release_project_engine_lease(project_id)
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
            self._release_project_engine_lease(project_id)
            return
        with self.state.db.connect() as connection:
            graph_store.release_project_lease(
                connection, project_id, self.state.instance_id, token
            )
        self._release_project_engine_lease(project_id)

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

    def _acquire_task(self, project_id: str) -> bool:
        if self._competition_project(project_id):
            return self.resources.acquire_coordinated_task(project_id)
        return self.resources.acquire_task(project_id)

    def startup_in_progress(self, project_id: str) -> bool:
        """Public read-only view for the competition coordinator."""
        return self._startup_in_progress(project_id)

    def _submit_redispatch(self, project_id: str) -> None:
        with self._lock:
            if project_id in self._startup_project_ids:
                return
            self._startup_project_ids.add(project_id)
        try:
            future = self.startup_executor.submit(self._redispatch_project, project_id)
        except Exception:
            with self._lock:
                self._startup_project_ids.discard(project_id)
            raise
        with self._lock:
            self._startup_futures[project_id] = future

    def _redispatch_project(self, project_id: str) -> None:
        try:
            if self.lifecycle.status(project_id) != "running":
                return
            self._dispatch_project(project_id)
        except Exception as exc:
            _recovery_logger.debug('Error handled in _redispatch_project', exc_info=True)
            self.state.logger.project(
                "project_redispatch_failed",
                project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            with self._lock:
                self._startup_project_ids.discard(project_id)
                self._startup_futures.pop(project_id, None)

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
        if not self._acquire_task(project_id):
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
            with self._lock:
                occupied = self._global_active_members_locked() | self._reserved_members
                assignment = self.diamond.assign_initial(
                    project_id,
                    occupied,
                    preferred_member=self._competition_preferred_member(project_id),
                )
                if assignment is not None:
                    self._reserved_members.add(assignment.member)
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
            try:
                launched = self._launch_member(
                    project_id,
                    assignment.member,
                    assignment.intent_id,
                    self._category(project_id),
                    assignment.is_initial,
                    reserved=True,
                )
            finally:
                with self._lock:
                    self._reserved_members.discard(assignment.member)
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
        if not self._acquire_task(project_id):
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


    def handle_report(self, project_id: str, report) -> None:
        if self.lifecycle.status(project_id) != "running":
            return
        self._broadcast_bump(project_id, report)
        self._publish_competition_recon(
            project_id,
            report.member,
            event_id=f"report:{report.id}",
            payload={
                "kind": "difficulty_report",
                "report": report.model_dump(mode="json")
                if hasattr(report, "model_dump") else str(report),
            },
        )
        available_slots = max(
            0,
            self.state.config.runtime.max_members_per_report - self._project_running_future_count(project_id),
        )
        assignments = self.diamond.decide_reinforcements(
            project_id,
            report,
            available_slots=available_slots,
        )
        category = self._category(project_id)
        for a in assignments:
            try:
                self._launch_member(project_id, a.member, a.intent_id, category, a.is_initial)
            except SandboxStartupError as exc:
                self._mark_project_startup_failure(
                    project_id,
                    exc,
                    event="project_sandbox_start_failed",
                )
                return

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

    def _publish_competition_recon(
        self,
        project_id: str,
        source_member: str,
        *,
        event_id: str,
        payload: dict[str, Any],
        receiver_member: str | None = None,
    ) -> list[dict]:
        """Bridge durable Member reports into the same-challenge PUB/SUB path."""
        competition = getattr(self.state, "competition", None)
        if competition is None or not callable(getattr(competition, "publish_recon", None)):
            return []
        now = datetime.now(timezone.utc)
        try:
            with self.state.db.connect() as connection:
                assignments = connection.execute(
                    """SELECT a.*
                       FROM competition_assignments a
                       JOIN competition_challenges c ON c.id=a.challenge_id
                       JOIN competition_runs r ON r.id=a.run_id
                       WHERE c.project_id=%s AND a.role<>'wp'
                         AND a.released_at IS NULL
                         AND r.status IN ('running','paused','draining')
                       ORDER BY a.position,a.id""",
                    (project_id,),
                ).fetchall()
            senders = [
                item for item in assignments
                if source_member is None or item["member"] == source_member
            ]
            if not senders:
                return []
            sender = senders[0]
            actual_source = sender["member"]
            published = []
            for receiver in assignments:
                if receiver["member"] == actual_source:
                    continue
                if receiver_member and receiver["member"] != receiver_member:
                    continue
                message = Envelope(
                    run_id=sender["run_id"],
                    challenge_id=sender["challenge_id"],
                    sender_member_id=actual_source,
                    receiver_member_id=receiver["member"],
                    sender_session_id=sender["session_id"],
                    receiver_session_id=receiver["session_id"],
                    request_id=f"{event_id}:{receiver['session_id']}"[:128],
                    correlation_id=event_id,
                    lease_epoch=sender["epoch"],
                    created_at=now,
                    deadline_at=now + timedelta(seconds=300),
                    message_type="recon",
                    payload=payload,
                )
                receipt = competition.publish_recon(message)
                published.append({"request_id": message.request_id, "receiver": receiver["member"],
                                  "sequence": receipt.get("sequence") if isinstance(receipt, dict) else None})
            return published
        except Exception as exc:
            _recovery_logger.debug('Error handled in _publish_competition_recon', exc_info=True)
            self.state.logger.project(
                "competition_recon_publish_failed",
                project_id,
                member=source_member,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    def _send_member_message(self, project_id: str, source: str, receiver: str, payload: dict) -> dict:
        event_id = "member:" + hashlib.sha256(json.dumps([project_id, source, receiver, payload], sort_keys=True).encode()).hexdigest()[:32]
        receipts = self._publish_competition_recon(project_id, source, event_id=event_id,
                                                   payload=payload, receiver_member=receiver)
        if not receipts:
            raise ValueError("recipient is not an active teammate on this challenge or message delivery is unavailable")
        return {"published": True, "receipts": receipts}

    def _broadcast_fact(self, project_id: str, fact_id: str, source_member: str | None) -> None:
        with self.state.db.connect() as conn:
            facts = {fact.id: fact.description for fact in node_store.list_facts(conn, project_id)}
        description = facts.get(fact_id)
        if not description:
            return
        self._publish_competition_recon(
            project_id,
            source_member,
            event_id=f"fact:{fact_id}:{hashlib.sha256(description.encode()).hexdigest()[:16]}",
            payload={
                "kind": "confirmed_fact",
                "fact_id": fact_id,
                "description": description,
                "source_member": source_member,
            },
        )
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


    def _member_config(self, name: str):
        for m in self.state.config.members:
            if m.name == name:
                return m
        return None

    def _competition_project(self, project_id: str) -> bool:
        try:
            with self.state.db.connect() as connection:
                return bool(connection.execute(
                    """SELECT 1 FROM competition_challenges c
                       JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                       JOIN competition_runs r ON r.id=rc.run_id
                       WHERE c.project_id=%s AND r.status NOT IN ('finished','stopped')
                       LIMIT 1""",
                    (project_id,),
                ).fetchone())
        except Exception:
            _recovery_logger.debug('Error handled in _competition_project', exc_info=True)
            return False

    def _competition_running(self, project_id: str) -> bool:
        with self.state.db.connect() as connection:
            return bool(connection.execute(
                """SELECT 1 FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   JOIN competition_runs r ON r.id=rc.run_id
                   WHERE c.project_id=%s AND r.status='running' LIMIT 1""",
                (project_id,),
            ).fetchone())

    def _competition_members(self, project_id: str) -> set[str] | None:
        """Return durable seats for a competition project, if one exists."""
        try:
            with self.state.db.connect() as connection:
                rows = connection.execute(
                    """SELECT DISTINCT a.member
                       FROM competition_assignments a
                       JOIN competition_challenges c ON c.id=a.challenge_id
                       JOIN competition_runs r ON r.id=a.run_id
                       WHERE c.project_id=%s AND a.role<>'wp'
                         AND a.released_at IS NULL
                         AND r.status NOT IN ('finished','stopped')""",
                    (project_id,),
                ).fetchall()
            return {row["member"] for row in rows}
        except Exception:
            _recovery_logger.debug('Error handled in _competition_members', exc_info=True)
            return None

    def _competition_preferred_member(self, project_id: str) -> str | None:
        """Use a persisted competition seat when bootstrapping a project."""
        try:
            with self.state.db.connect() as connection:
                row = connection.execute(
                    """SELECT a.member FROM competition_assignments a
                       JOIN competition_challenges c ON c.id=a.challenge_id
                       JOIN competition_runs r ON r.id=a.run_id
                       WHERE c.project_id=%s AND a.role<>'wp'
                         AND a.released_at IS NULL
                         AND r.status IN ('running','paused','draining')
                       ORDER BY a.position,a.id LIMIT 1""",
                    (project_id,),
                ).fetchone()
            return row["member"] if row else None
        except Exception:
            _recovery_logger.debug('Error handled in _competition_preferred_member', exc_info=True)
            return None

    def _competition_assignment(self, project_id: str, member_name: str) -> dict | None:
        """Load the active durable assignment for a Member/project pair."""
        try:
            with self.state.db.connect() as connection:
                return connection.execute(
                    """SELECT a.* FROM competition_assignments a
                       JOIN competition_challenges c ON c.id=a.challenge_id
                       JOIN competition_runs r ON r.id=a.run_id
                       WHERE c.project_id=%s AND a.member=%s AND a.role<>'wp'
                         AND a.released_at IS NULL AND r.status='running'
                       ORDER BY a.position,a.id LIMIT 1""",
                    (project_id, member_name),
                ).fetchone()
        except Exception:
            _recovery_logger.debug('Error handled in _competition_assignment', exc_info=True)
            return None

    def _global_active_members_locked(self) -> set[str]:
        return {
            name for project_members in self._members.values()
            for name in project_members
        }

    def active_member_owners(self) -> dict[str, str]:
        """Return a stable Member -> project snapshot for competition sync."""
        with self._lock:
            return {
                name: project_id
                for project_id, project_members in self._members.items()
                for name in project_members
            }

    def _throttled_log(
        self, event: str, project_id: str, key: str, **fields: Any
    ) -> None:
        """Emit ``event`` for ``(project, key)`` with exponential-backoff folding.

        A steady-state rejection logged on every tick drowns the log. This emits
        the first occurrence, then backs off 1, 2, 4 ... seconds (capped at 300)
        and folds suppressed hits into an ``attempts`` count on the next line
        that fires. The suppressed window is time-based, so a burst collapses to
        a logarithmic number of lines regardless of tick rate.
        """
        throttle_key = (event, key)
        count = self._throttle_counts.get(throttle_key, 0) + 1
        self._throttle_counts[throttle_key] = count
        now = monotonic()
        due = self._throttle_next_log.get(throttle_key)
        if due is not None and now < due:
            return
        emitted = self._throttle_emitted.get(throttle_key, 0)
        delay = min(2.0 ** emitted, 300.0)
        self._throttle_next_log[throttle_key] = now + delay
        self._throttle_emitted[throttle_key] = emitted + 1
        self.state.logger.project(
            event, project_id,
            attempts=count, next_log_in_seconds=round(delay, 1),
            **fields,
        )

    def _log_legacy_block(self, project_id: str, member_name: str, intent_id: str) -> None:
        """Rate-limit the ``legacy_member_blocked_by_competition`` event.

        While a competition owns a project, every legacy dispatch attempt for a
        non-seated Member is rejected here.  That is expected steady state, but
        logging it on each attempt produced thousands of identical lines (one
        project emitted one per second).  Emit the first occurrence per
        (project, member, intent), then back off exponentially (1, 2, 4, ...
        seconds, capped at 300) and fold the suppressed attempts into an
        ``attempts`` count on the next line that does fire.
        """
        key = (project_id, member_name, intent_id or "")
        count = self._legacy_block_counts.get(key, 0) + 1
        self._legacy_block_counts[key] = count
        now = monotonic()
        due = self._legacy_block_next_log.get(key)
        if due is not None and now < due:
            return
        emitted = self._legacy_block_emitted_counts.get(key, 0)
        delay = min(2.0 ** emitted, 300.0)
        self._legacy_block_next_log[key] = now + delay
        self._legacy_block_emitted_counts[key] = emitted + 1
        self.state.logger.project(
            "legacy_member_blocked_by_competition",
            project_id,
            member=member_name,
            intent=intent_id,
            attempts=count,
            next_log_in_seconds=round(delay, 1),
        )

    def _launch_member(
        self, project_id, member_name, intent_id, category, is_initial,
        *, reserved: bool = False,
    ) -> bool:
        if self._stop.is_set():
            return False
        if self._competition_project(project_id):
            seats = self._competition_members(project_id)
            if seats is not None and member_name not in seats:
                self._log_legacy_block(project_id, member_name, intent_id)
                return False
        cfg = self._member_config(member_name)
        if cfg is None:
            return False
        with self._lock:
            occupied = self._global_active_members_locked()
            if member_name in occupied:
                return False
            if not reserved:
                if member_name in self._reserved_members:
                    return False
                self._reserved_members.add(member_name)
        try:
            assignment = self._record_member_assignment(
                project_id, member_name, intent_id
            )
        except Exception:
            if not reserved:
                with self._lock:
                    self._reserved_members.discard(member_name)
            raise
        if assignment is None:
            if not reserved:
                with self._lock:
                    self._reserved_members.discard(member_name)
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
            competition_assignment = (
                self._competition_assignment(project_id, member_name)
                if self._competition_project(project_id) else None
            )
            competition_store = None
            competition_service = getattr(self.state, "competition", None)
            job_store = None
            if competition_assignment is not None:
                from backend.agent.jobs import AgentJobStore
                from backend.competition.store import CompetitionStore

                competition_store = CompetitionStore(self.state.db)
                job_store = AgentJobStore(self.state.db)
            deps = MemberDeps(
                db=self.state.db,
                logger=self.state.logger,
                sandbox=sandbox,
                mcps=self.state.mcps,
                registry=self.state.registry,
                memory=self.state.memory,
                container_mcps=self._container_mcps(project_id, member_name),
                eval_interval=self.state.config.runtime.eval_interval_steps,
                max_steps=self.state.config.runtime.max_member_steps,
                max_actions_per_task=self.state.config.runtime.max_member_actions_per_task,
                continuous=self._competition_project(project_id),
                on_report=self.handle_report,
                on_fact=self._broadcast_fact,
                on_message=self._send_member_message,
                on_flag=self.on_flag_found,
                lease_owner=lease_owner,
                lease_token=lease_token,
                competition_store=competition_store,
                competition_assignment=competition_assignment,
                competition_native=competition_assignment is not None,
                competition_recon_endpoint=(
                    competition_service.recon_endpoint
                    if competition_service is not None else None
                ),
                competition_recon_replay=(
                    competition_store.replay_messages
                    if competition_store is not None else None
                ),
                competition_recon_advance=(
                    competition_store.advance_message_cursor
                    if competition_store is not None else None
                ),
                runtime=self.state.config.runtime,
                job_store=job_store,
            )
            script = self.scripts.get((project_id, member_name)) or self.scripts.get(member_name)
            member = create_member(cfg, deps, script=script)
            with self._lock:
                stopping = self._stop.is_set()
                if not stopping:
                    self._members.setdefault(project_id, {})[member_name] = member
                if not reserved:
                    self._reserved_members.discard(member_name)
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
            with self._lock:
                if not reserved:
                    self._reserved_members.discard(member_name)
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
            seat = getattr(member.deps, "competition_assignment", None)
            if seat is not None:
                member.deps.competition_store.append_run_event(
                    seat["run_id"], "member.started",
                    {"assignment_id": seat["id"], "challenge_id": seat["challenge_id"],
                     "session_id": seat["session_id"], "member": member.name,
                     "intent_id": intent_id, "epoch": seat["epoch"]},
                )
            return asyncio.run(
                member.solve_async(project_id, intent_id, category, is_initial=is_initial)
            )
        except Exception as exc:
            _recovery_logger.debug('Error handled in _run_member', exc_info=True)
            self.state.logger.project("member_crash", project_id, member=member.name, error=str(exc))
        finally:
            with self._lock:
                self._members.get(project_id, {}).pop(member.name, None)
            with self.state.db.connect() as conn:
                row = graph_store.get_project_row(conn, project_id)
                if row is not None and row["status"] == "running":
                    graph_store.set_agent_state(conn, project_id, member.name, "idle")


    def stop_member(self, member_name: str) -> bool:
        """Cancel one Member's in-flight work without touching its project.

        The watchdog's middle escalation (P5c): the Member's durable session,
        its seat and everything it has recorded survive, and only the wedged
        turn is abandoned. ``stop_project`` would be far too blunt here - it
        tears down the whole challenge the Member was working on.
        """
        with self._lock:
            targets = [
                members[member_name]
                for members in self._members.values()
                if member_name in members
            ]
        for member in targets:
            member.stop()
        return bool(targets)

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
        status = self.lifecycle.status(project_id)
        if status in ("created", "running"):
            with suppress(LifecycleError):
                self.lifecycle.transition(project_id, "stopped")
        phase = "solved" if status == "solved" else "stopped"
        self._set_runtime_phase(project_id, phase)
        self._release_project_lease(project_id)


    def on_flag_found(self, project_id: str) -> None:
        if self._competition_project(project_id):
            competition = getattr(self.state, "competition", None)
            if competition is not None:
                competition.wake()
            return
        with self._lock:
            if project_id in self._completing:
                return
            self._completing.add(project_id)
        try:
            async_verdict = self._finalize(project_id)
        except Exception:
            with self._lock:
                self._completing.discard(project_id)
            raise
        if not async_verdict:
            with self._lock:
                self._completing.discard(project_id)

    def _finalize(self, project_id: str) -> bool:
        """Close out a reported Flag.

        Returns True when an asynchronous platform-verdict job took over and
        now owns the ``_completing`` guard; the job clears it on exit.
        """

        with self._lock:
            members = list(self._members.get(project_id, {}).values())
        for member in members:
            member.stop()

        if self.lifecycle.status(project_id) == "running":
            with suppress(LifecycleError):
                self.lifecycle.transition(project_id, "flag_found")

        if self._platform_verdict_needed(project_id):
            return self._dispatch_platform_verdict(project_id)
        self._finalize_local(project_id)
        return False

    def _finalize_local(self, project_id: str) -> None:
        state = self.state
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
        self._commit_solved(project_id, verdict["flag"], source="orchestrator")

    def _commit_solved(self, project_id: str, flag: str, *, source: str) -> None:
        """Accept a verified Flag and run the solved close-out pipeline."""

        state = self.state
        with state.db.connect() as connection:
            solved = accept_verified_flag(
                connection,
                project_id,
                flag,
                source=source,
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


    def _platform_verdict_needed(self, project_id: str) -> bool:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
        return requires_platform_verdict(row)

    def _dispatch_platform_verdict(self, project_id: str) -> bool:
        with self.state.db.connect() as connection:
            project = graph_store.get_project_row(connection, project_id)
        if not project or project["platform"] != "ret2shell" or getattr(self.state, "ret2shell_client", None) is None:
            with self._lock:
                already_logged = project_id in self._verdict_deferred_logged
                self._verdict_deferred_logged.add(project_id)
            if not already_logged:
                self.state.logger.project(
                    "platform_verdict_deferred",
                    project_id,
                    reason="platform verdict adapter or credentials are not configured",
                )
            return False
        with self._lock:
            self._verdict_deferred_logged.discard(project_id)
        self._set_runtime_phase(project_id, "judging")
        self.executor.submit(self._run_platform_verdict, project_id)
        return True

    def _run_platform_verdict(self, project_id: str) -> None:
        try:
            self._judge_pending_submissions(project_id)
        except Exception as exc:
            _recovery_logger.debug('Error handled in _run_platform_verdict', exc_info=True)
            self.state.logger.project(
                "platform_verdict_crashed",
                project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            with suppress(Exception), self.state.db.connect() as conn:
                conn.execute(
                    "UPDATE flag_submissions SET status = 'unknown' "
                    "WHERE project_id = %s AND status = 'judging'",
                    (project_id,),
                )
        finally:
            with self._lock:
                self._completing.discard(project_id)

    def _judge_pending_submissions(self, project_id: str) -> None:
        client = self.state.ret2shell_client
        while not self._stop.is_set():
            with self.state.db.connect() as conn:
                conn.execute(
                    "UPDATE flag_submissions SET status = 'unknown' "
                    "WHERE project_id = %s AND status = 'judging'",
                    (project_id,),
                )
                if conn.execute(
                    "SELECT 1 FROM flag_submissions WHERE project_id=%s AND status='unknown' LIMIT 1",
                    (project_id,),
                ).fetchone():
                    self.state.logger.project("platform_verdict_needs_reconciliation", project_id)
                    return
                submission = conn.execute(
                    """
                    UPDATE flag_submissions SET status = 'judging'
                    WHERE id = (
                        SELECT id FROM flag_submissions
                        WHERE project_id = %s AND status = 'pending'
                        ORDER BY id LIMIT 1
                    )
                    RETURNING id, normalized_flag
                    """,
                    (project_id,),
                ).fetchone()
                row = graph_store.get_project_row(conn, project_id)
            if submission is None:
                self._resume_after_verdict(project_id, reason="no pending flag submissions")
                return
            external_id = str(row["external_id"] or "") if row else ""
            verdict, detail = self._submit_to_platform(
                client, external_id, submission["normalized_flag"]
            )
            if verdict == "correct":
                self._verdict_unknown_attempts.pop(project_id, None)
                self._commit_solved(
                    project_id,
                    submission["normalized_flag"],
                    source="platform:ret2shell",
                )
                return
            if verdict == "wrong":
                self._verdict_unknown_attempts.pop(project_id, None)
                self._reject_submission(project_id, row, submission, detail)
                continue
            with self.state.db.connect() as conn:
                attempts = self._verdict_unknown_attempts.get(project_id, 0) + 1
                self._verdict_unknown_attempts[project_id] = attempts
                if attempts < self._VERDICT_MAX_UNKNOWN_ATTEMPTS:
                    conn.execute(
                        "UPDATE flag_submissions SET status='pending',error=%s WHERE id=%s",
                        (detail, submission["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE flag_submissions SET status='error',error=%s WHERE id=%s",
                        (detail, submission["id"]),
                    )
            if attempts < self._VERDICT_MAX_UNKNOWN_ATTEMPTS:
                self.state.logger.project(
                    "platform_verdict_retry_deferred", project_id,
                    error=detail, attempt=attempts,
                )
            else:
                self._verdict_unknown_attempts.pop(project_id, None)
                self._resume_after_verdict(
                    project_id, reason="platform verdict retries exhausted"
                )
            return

    def _submit_to_platform(self, client, external_id: str, flag: str) -> tuple[str, str]:
        """Ask the platform judge about one candidate.

        Returns ``(verdict, detail)`` where verdict is ``correct``, ``wrong``
        or ``unknown`` (transient failure: quota, network, judge timeout).
        """

        try:
            challenge_id = int(external_id)
        except (TypeError, ValueError):
            return "unknown", f"external_id {external_id!r} is not a ret2shell challenge id"
        try:
            submission = client.submit_flag(challenge_id, flag)
        except Ret2ShellPreflightError as exc:
            if "already solved" in str(exc):
                return "unknown", "platform already solved externally; candidate remains unverified"
            return "unknown", f"submit preflight failed: {exc}"
        except Ret2ShellRateLimitError as exc:
            return "unknown", str(exc)
        except Ret2ShellError as exc:
            return "unknown", f"platform submit failed: {exc}"
        except Exception as exc:
            _recovery_logger.debug('Error handled in _submit_to_platform', exc_info=True)
            return "unknown", f"{type(exc).__name__}: {exc}"
        solved = submission.get("solved") if isinstance(submission, dict) else None
        if solved is True:
            return "correct", str(submission.get("result") or "accepted")
        if solved is False:
            return "wrong", str(submission.get("result") or "rejected")
        return "unknown", "platform judge did not answer before the poll timeout"

    def _reject_submission(self, project_id: str, project_row, submission, detail: str) -> None:
        with self.state.db.connect() as conn:
            conn.execute(
                "UPDATE flag_submissions SET status = 'rejected', error = %s WHERE id = %s",
                (detail, submission["id"]),
            )
        self.state.logger.project(
            "platform_flag_rejected",
            project_id,
            external_id=project_row["external_id"] if project_row else None,
            verdict=detail,
        )
        if project_row is not None:
            try:
                category = str(project_row["category"] or "misc")
                self.state.memory.add(
                    "lessons",
                    title=f"{project_row['title']}: platform rejected flag",
                    content=(
                        f"Platform rejected candidate flag '{submission['normalized_flag']}' "
                        f"for challenge '{project_row['title']}' "
                        f"(external_id={project_row['external_id']}). Verdict: {detail}"
                    ),
                    tags=[category, "rejected-flag"],
                    project_id=project_id,
                    source="platform",
                )
            except Exception as exc:
                _recovery_logger.debug('Error handled in _reject_submission', exc_info=True)
                self.state.logger.project(
                    "rejected_flag_memory_failed",
                    project_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _resume_after_verdict(self, project_id: str, *, reason: str) -> None:
        if self.lifecycle.status(project_id) != "flag_found":
            return
        with suppress(LifecycleError):
            self.lifecycle.transition(project_id, "running")
        self._set_runtime_phase(project_id, "running")
        self.state.logger.project("platform_verdict_resumed", project_id, reason=reason)

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
                _recovery_logger.debug('Error handled in _drain_postprocess_jobs', exc_info=True)
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
                self._commit_postprocess_job(job, prepared)
            except PostprocessLeaseLost:
                self._log_stale_postprocess_job(job, stage="commit")
            except Exception as exc:
                _recovery_logger.debug('Error handled in _drain_postprocess_jobs', exc_info=True)
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
                _recovery_logger.debug('Error handled in _heartbeat_postprocess_job', exc_info=True)
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
                lock_project_completion(connection, job.project_id)
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
                if row is not None and row["postprocess_status"] == "completed":
                    self.diamond.draw_completion(job.project_id, connection=connection)
        except Exception:
            if rollback_file is not None:
                rollback_file()
            raise
        if written_memories and self.state.memory.export_dir is not None:
            for memory in written_memories:
                try:
                    self.state.memory._mirror_to_disk(memory)
                except Exception as exc:
                    _recovery_logger.debug('Error handled in _commit_postprocess_job', exc_info=True)
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


    def _run_loop(self) -> None:
        interval = max(1, self.state.config.runtime.interval)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                _recovery_logger.debug('Error handled in _run_loop', exc_info=True)
                try:
                    self.state.logger.project("scheduler_error", "system", error=str(exc))
                except Exception:
                    _recovery_logger.debug('Error handled in _run_loop', exc_info=True)
                    print(f"[ipc-orchestrator] tick failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._stop.wait(interval)

    def _competition_project_ownership(
        self, conn
    ) -> tuple[set[str], set[str]]:
        """Partition competition-linked projects into active vs. terminal (T4).

        ``active`` projects are owned by a live competition run through the
        coordinator; the generic recovery loop must leave them alone. ``terminal``
        projects are linked only to runs that have stopped or finished - a graph
        status still reading ``running`` there is stale, and resurrecting a
        session is the stop-then-restart bug. Any query failure (an old schema
        opened before the additive patch) returns empty sets, preserving the
        prior fail-open behaviour rather than blocking recovery outright.
        """
        active: set[str] = set()
        terminal: set[str] = set()
        try:
            for row in conn.execute(
                """SELECT c.project_id,
                          bool_or(r.status NOT IN ('finished','stopped')) AS has_active
                   FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   JOIN competition_runs r ON r.id=rc.run_id
                   WHERE c.project_id IS NOT NULL
                   GROUP BY c.project_id"""
            ).fetchall():
                if row["has_active"]:
                    active.add(row["project_id"])
                else:
                    terminal.add(row["project_id"])
        except Exception:
            _recovery_logger.debug('Error handled in _competition_project_ownership', exc_info=True)
            return set(), set()
        return active, terminal

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
            competition_projects, competition_terminal_projects = (
                self._competition_project_ownership(conn)
            )
        self._initialize_reason_checkpoints(summaries)
        for summary in summaries:
            if summary.id in competition_projects:
                if summary.status == "running" and not self._startup_in_progress(summary.id):
                    try:
                        self._dispatch_project(summary.id)
                    except Exception as exc:
                        _recovery_logger.debug('Error handled in _tick', exc_info=True)
                        self.state.logger.project("scheduler_error", summary.id, error=str(exc))
                continue
            if summary.id in competition_terminal_projects:
                continue
            if summary.status == "flag_found":
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
            if self._startup_in_progress(summary.id):
                continue
            if not self._acquire_task(summary.id):
                self._queue_project(summary.id)
                continue
            try:
                self._dispatch_project(summary.id)
            except Exception as exc:
                _recovery_logger.debug('Error handled in _tick', exc_info=True)
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
            _recovery_logger.debug('Error handled in _schedule_running_recovery', exc_info=True)
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
            if not self._acquire_task(project_id):
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
            _recovery_logger.debug('Error handled in _recover_running_project', exc_info=True)
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
        with self._lock:
            project_tasks = {k: f for k, f in self._task_index.items() if k[0] == project_id and not f.done()}
            active_members = set(self._members.get(project_id, {}).keys())
        max_running = min(2, self.state.config.runtime.max_members_per_report)
        available_slots = max_running - len(project_tasks)
        if available_slots <= 0:
            return
        with self.state.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            return
        running = {intent_id for (pid, intent_id), fut in project_tasks.items() if pid == project_id and not fut.done()}
        open_intents = [i for i in detail.intents if i.to is None]
        category = detail.project.category
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
                self._dispatch_project(project_id)
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
            if not self._competition_project(project_id) and not self.resources.can_admit_task(project_id):
                return
            self._remove_queued_project(project_id)
            status = self.lifecycle.status(project_id)
            if status == "created" or status == "stopped":
                self.start_project_async(project_id)
            elif status == "running" and (
                not self._startup_in_progress(project_id)
                and self._ensure_project_lease(project_id)
            ):
                self._schedule_running_recovery(project_id)

    def _select_member_for_intent(self, project_id: str, detail, intent, active: set[str]) -> str | None:
        preferred = self._intent_member_candidates(detail, intent)
        allowed = None
        if self._competition_project(project_id):
            allowed = self._competition_members(project_id)
            if allowed is not None:
                preferred = [name for name in preferred if name in allowed]
        return self._pick_idle_member(
            project_id, detail=detail, active=active, preferred=preferred, allowed=allowed,
        )

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
        allowed: set[str] | None = None,
    ) -> str | None:
        if active is None:
            with self._lock:
                active = set(self._members.get(project_id, {}).keys())
        with self._lock:
            globally_occupied = self._global_active_members_locked() | self._reserved_members
        idle = [
            member.name for member in self.state.config.available_members()
            if member.name not in active and member.name not in globally_occupied
        ]
        if allowed is not None:
            idle = [name for name in idle if name in allowed]
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
        configured = {member.name for member in self.state.config.available_members()}
        allocation_order = [
            name for name in ("aventurine", "pearl") if name in configured
        ]
        allocation_order.extend(
            member.name for member in self.state.config.available_members()
            if member.name not in allocation_order
        )
        config_order = {name: idx for idx, name in enumerate(allocation_order)}
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
                _recovery_logger.debug('Error handled in _reap_finished_futures', exc_info=True)
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
                    self._flag_followup_from_fact(project_id, result.fact_id)
            elif result.status == "flag":
                self._clear_member_stalls(project_id, intent_id)
                self.state.logger.project("member_task_flag", project_id, intent=intent_id, flag=result.flag)

    def _flag_followup_from_fact(self, project_id: str, fact_id: str) -> None:
        """Turn a flag-shaped conclusion into an explicit submit tool task.

        A prose ``conclude`` is evidence only.  The follow-up intent keeps the
        submission contract enforceable while avoiding an automatic network
        submission when a conclusion contains multiple candidates.
        """
        with self.state.db.connect() as connection:
            connection.execute(
                "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,)
            )
            row = connection.execute(
                "SELECT description FROM facts WHERE project_id=%s AND id=%s",
                (project_id, fact_id),
            ).fetchone()
            if row is None:
                return
            text = row["description"]
            if re.search(r"\b(?:example|placeholder|sample flag)\b|示例|占位", text, re.IGNORECASE):
                return
            candidates = sorted(set(re.findall(r"(?:[A-Za-z0-9_]*ctf|flag)\{[^}\n]{1,400}\}", text, re.IGNORECASE)))
            if len(candidates) != 1:
                if len(candidates) > 1:
                    self.state.logger.project(
                        "flag.multiple_candidates_in_fact", project_id, fact_id=fact_id,
                        candidates=len(candidates),
                    )
                return
            candidate = candidates[0]
            existing = connection.execute(
                "SELECT 1 FROM flag_submissions WHERE project_id=%s AND normalized_flag=%s LIMIT 1",
                (project_id, candidate),
            ).fetchone()
            if existing:
                return
            description = "Submit the verified flag with the submit_flag tool; do not conclude prose"
            open_intent = edge_store.find_similar_open_intent(
                connection, project_id, [fact_id], description,
            )
            if open_intent is None:
                edge_store.create_intent(
                    connection, project_id, [fact_id],
                    description,
                    "orchestrator",
                )
            self.state.logger.project(
                "flag.found_in_fact_without_submission", project_id,
                fact_id=fact_id, candidate=candidate,
            )
            competition = connection.execute(
                """SELECT c.run_id,c.id AS challenge_id FROM competition_challenges c
                   WHERE c.project_id=%s ORDER BY c.id LIMIT 1""",
                (project_id,),
            ).fetchone()
            if competition:
                connection.execute(
                    """INSERT INTO competition_run_events (run_id,kind,payload)
                       VALUES (%s,'flag.found_in_fact_without_submission',%s)""",
                    (competition["run_id"], Jsonb({
                        "challenge_id": competition["challenge_id"],
                        "fact_id": fact_id,
                        "candidate": candidate,
                    })),
                )

    def _intent_worker(self, project_id: str, intent_id: str) -> str | None:
        with self.state.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            return row["worker"] if row is not None else None


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
