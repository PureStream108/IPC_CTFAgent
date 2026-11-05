from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.errors import UndefinedColumn, UndefinedTable
from psycopg.types.json import Jsonb

from backend.agent.jobs import AgentJobStore
from backend.blackboard import edge_store, node_store
from backend.competition.local import LocalPlatform
from backend.competition.platform import CompetitionPlatform
from backend.competition.policy import (
    Challenge,
    park_backoff_minutes,
    park_candidates,
    phase_for,
    plan_assignments,
)
from backend.competition.store import (
    SERVICE_MEMBER_ID,
    CompetitionConflict,
    CompetitionStore,
)
from backend.competition.transport import Envelope, PairCoordinator, ReconPublisher
from backend.core.ipc import accept_verified_flag
from backend.core.wp_writer import (
    WRITEUP_SYSTEM_PROMPT,
    generate_wp_content,
    persist_validated_writeup,
)
from backend.members.adapters import make_adapter
from backend.ops.models import PlatformWorkflowSpec
from backend.ops.service import OpsAgentService

_recovery_logger = logging.getLogger(__name__)


def writeups_enabled() -> bool:
    """Whether solved challenges get a writeup during a competition run.

    A scored benchmark gets nothing from one: the platform scores flags, while
    a writeup spends a model call and, for the duration of the job, one of the
    Member seats the scheduler would otherwise use as a helper. Deployments
    that want the artifact (an operator-driven contest, a report) keep the
    default; a timed benchmark sets IPC_WP_JOBS=0.
    """

    value = os.environ.get("IPC_WP_JOBS")
    if value is None or not value.strip():
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


PARK_DIGEST_SYSTEM_PROMPT = (
    "You are the handoff writer between two attempts at the same CTF challenge. "
    "The first attempt ran out of its time budget; its container is gone but its transcript survives. "
    "The next attempt starts in a clean container and may be a different Member. "
    "Report only what the transcript proves. Quote exact paths, commands, ports, hashes and credentials. "
    "A wrong fact in a handoff is worse than an empty one."
)


def _fallback_park_digest(row: dict, events: list[dict], attempts: int, backoff: int) -> str:
    """Deterministic note used when no model is configured or the call fails."""

    commands: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        arguments = payload.get("arguments") or payload.get("args") or {}
        if isinstance(arguments, dict):
            command = str(arguments.get("command") or "").strip()
            if command:
                commands.append(command.replace(chr(10), " ")[:140])
    metadata = row.get("metadata") or {}
    address = ""
    for key in ("container_addr", "container_address", "entry", "address"):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)) and value:
            address = ", ".join(str(item) for item in value)
            break
        if isinstance(value, str) and value.strip():
            address = value.strip()
            break
    lines = [
        "【park 交接 · 第 " + str(int(attempts)) + " 次】" + str(row.get("external_id") or ""),
        "【已确认事实】本次尝试在软截止处被回收，冷却约 " + str(int(backoff))
        + " 分钟后重试；容器已关闭，工作区文件与会话记录保留。会话事件数：" + str(len(events)) + "。",
        "【已排除方向】见下列失败命令与会话记录；不要重复已证明不通的方向。",
        "【最后命令与输出】",
    ]
    for command in commands[-6:]:
        lines.append("- " + command)
    if not commands:
        lines.append("- （未能从会话中提取命令，请先读 /workspace/shared 下的证据文件）")
    lines.append("【容器地址与端口】" + (
        address or "容器已关闭，地址已回收；重试会拿到新地址。"
    ))
    lines.append("【下一步建议】先复核 /workspace/shared 证据，再选一条与上述命令不重复的方向。")
    return chr(10).join(lines)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(low, min(high, value))


def hint_at_attempt() -> int:
    """Which park buys the platform hint. 0 disables buying entirely.

    Two parks means the challenge already burned 45 minutes plus a ten minute
    cooldown and came back, so the next attempt is the right place to spend a
    score fraction on a hint. IPC_HINT_AT_ATTEMPT=1 buys at the first park
    instead (55 minutes earlier).
    """

    return _env_int("IPC_HINT_AT_ATTEMPT", 2, 0, 9)


def hint_min_remaining_minutes() -> int:
    """Never buy a hint that the run has no window left to use."""

    return _env_int("IPC_HINT_MIN_REMAINING_MINUTES", 45, 0, 600)


def park_digests_enabled() -> bool:
    """Whether a parked challenge gets a consolidated handoff note.

    Parking closes the container but keeps the transcript, so the cooldown is
    dead time unless something turns that transcript into a short brief the
    next attempt reads first. IPC_WP_JOBS=0 (a scored benchmark) does not turn
    this off: the digest costs one small call and saves a whole re-exploration.
    Set IPC_PARK_DIGEST=0 to disable it entirely.
    """

    value = os.environ.get("IPC_PARK_DIGEST")
    if value is None or not value.strip():
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


TERMINAL_RUN_STATES = {"finished", "stopped"}


def _timestamp(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _select_discovered(discovered: list, select: list[str] | None) -> list:
    """Limit one run to explicitly requested challenges.

    ``None`` keeps every discovered challenge; an explicit list must be a
    subset of the preflight result so a typo cannot silently start a run with
    fewer challenges than the operator asked for.
    """
    if select is None:
        return discovered
    wanted = {str(item) for item in select}
    available = {item.external_id for item in discovered}
    missing = sorted(wanted - available)
    if missing:
        raise ValueError(
            "selected challenges are not available: " + ", ".join(missing)
        )
    return [item for item in discovered if item.external_id in wanted]


def _selected_external_ids(run: dict) -> set[str] | None:
    """Return the run's durable challenge selection, if one was requested."""

    snapshot = run.get("config_snapshot") or {}
    selected = snapshot.get("selected_external_ids")
    if selected is None:
        return None
    return {str(item) for item in selected}


def _safe_member_config(config) -> dict[str, Any] | None:
    """Return the durable/public part of the shared Member configuration.

    API credentials are process/deployment secrets.  A run snapshot is both
    persisted and returned by the run API, so storing the raw key there would
    make a normal status request a credential disclosure.  The live config is
    still used for model calls after a restart; the snapshot only records the
    shape and a non-reversible identity of the configured key.
    """
    if config is None:
        return None
    data = config.model_dump(mode="json")
    api_key = str(data.pop("api_key", "") or "")
    data["api_key_set"] = bool(api_key)
    if api_key:
        data["api_key_fingerprint"] = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return data


class CompetitionService:
    """Persistent competition coordinator.

    Network and model work always happens outside store transactions.  A run
    lease ensures only one application process performs periodic work, while
    the database still rechecks every assignment and submission transition.
    """

    def __init__(
        self,
        state,
        *,
        sync_interval: int | None = None,
        tick_interval: float = 1.0,
        platform_factory: Callable[[str, PlatformWorkflowSpec], Any] | None = None,
        ops_service: OpsAgentService | None = None,
    ):
        self.state = state
        self.store = CompetitionStore(state.db)
        self.watch_config = getattr(getattr(state, "config", None), "watch", None)
        # A run re-syncs its platform on this cadence, which is what lets it
        # import challenges published after Start.  The deployment decides how
        # impatient that should be; 120s is the historical default.
        configured = int(getattr(self.watch_config, "sync_seconds", 0) or 0)
        self.sync_interval = max(5, sync_interval if sync_interval is not None else (configured or 120))
        self.watcher = None
        self.tick_interval = max(0.1, tick_interval)
        self.owner = f"{state.instance_id}:competition"
        self.engine_owner = self.owner
        state.competition_engine_owner = self.engine_owner
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._starting_projects: set[str] = set()
        self._starting_members: set[tuple[str, str]] = set()
        self._pair: PairCoordinator | None = None
        self._recon: ReconPublisher | None = None
        self._recon_endpoint: str | None = None
        self._platform_factory = platform_factory
        self._ops_service = ops_service
        self._wp_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ipc-wp")
        self._wp_futures: dict[str, Any] = {}
        self._digest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ipc-park-digest")
        self._digest_futures: dict[str, Any] = {}
        # Container teardown and lookahead are slow platform calls: they run off
        # the tick thread, bounded, so one closing target never stalls the
        # scheduler that could be starting the next one.
        self._stop_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ipc-instance-stop")
        self._prestart_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ipc-prestart")
        self._prestart_futures: dict[tuple[str, str], Any] = {}
        self._slot_free_at: dict[str, float] = {}
        self.park_digests_enabled = park_digests_enabled()
        # The handoff must be durable before the challenge becomes ready again;
        # waiting for a slow model call is capped here and the deterministic
        # template takes over, so a park never hangs the tick.
        self.park_digest_timeout = float(
            _env_int("IPC_PARK_DIGEST_TIMEOUT", 90, 5, 600)
        )
        self.hint_at_attempt = hint_at_attempt()
        self.hint_min_remaining_minutes = hint_min_remaining_minutes()
        self._instance_capability_warnings: set[tuple[str, str]] = set()
        self._published_instance_entries: set[tuple[str, str, str, int]] = set()
        self._engine_conflicts: set[tuple[str, str]] = set()
        self._engine_epochs: dict[str, int] = {}
        self.observation_interval = 10.0
        self._last_observation_at = 0.0
        self._reported_stalls: set[str] = set()
        self._watchdog_actions: set[tuple[str, str]] = set()
        self._message_delivery_window = 300
        self._undelivered_injected: set[int] = set()
        self._receiver_cursors: dict[str, int] = {}
        self._stale_job_seconds = 1800
        runtime = getattr(state.config, "runtime", None)
        self.per_challenge_minutes = float(getattr(runtime, "per_challenge_minutes", 45) or 45)
        self._resource_capacity: dict[str, dict] = {}
        self.phase_metrics: dict[str, dict] = {}
        self._closed_runs: dict[str, str] = {}

    def _phase(self, name: str, operation, *args, **kwargs):
        started = time.monotonic()
        try:
            return operation(*args, **kwargs)
        finally:
            previous = self.phase_metrics.get(name, {})
            duration = round(time.monotonic()-started, 6)
            self.phase_metrics[name] = {"last_seconds": duration, "max_seconds": max(duration, previous.get("max_seconds", 0)),
                                        "calls": previous.get("calls", 0)+1}

    @property
    def ops(self) -> OpsAgentService:
        if self._ops_service is not None:
            return self._ops_service
        service = getattr(self.state, "ops_agent_service", None)
        if service is None:
            service = OpsAgentService(self.state)
            self.state.ops_agent_service = service
        return service

    def _start_watcher(self) -> None:
        """Attach the platform watcher once, when the deployment enabled it."""
        if self.watcher is None:
            watch = self.watch_config
            if watch is None or not getattr(watch, "enabled", False):
                return
            from backend.competition.watcher import PlatformWatcher

            self.watcher = PlatformWatcher(
                self,
                interval=getattr(watch, "interval_seconds", 60),
                workflow_ids=getattr(watch, "workflows", ()) or (),
            )
        self.watcher.start()

    def start_worker(self) -> None:
        self._start_watcher()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._start_loop_thread()
        if self._supervisor is None or not self._supervisor.is_alive():
            self._supervisor = threading.Thread(
                target=self._supervise, name="ipc-competition-sup", daemon=True
            )
            self._supervisor.start()
        if os.name == "posix" and self._pair is None:
            socket_path = self.state.artifact_root / "ipc" / "pair.sock"
            self._pair = PairCoordinator(
                socket_path,
                authorize=self.store.authorize_message,
                execute=self._execute_pair_message,
                claim=self.store.claim_message,
                finish=self.store.finish_message,
            )
            self._pair.start()
        if os.name == "posix" and self._recon is None:
            recon_path = self.state.artifact_root / "ipc" / "recon.sock"
            recon_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            endpoint = f"ipc://{recon_path}"
            if len(endpoint.encode()) <= 100:
                try:
                    self._recon = ReconPublisher(
                        endpoint,
                        self.store.persist_recon_message,
                    )
                    self._recon_endpoint = endpoint
                except Exception:
                    _recovery_logger.debug('Error handled in start_worker', exc_info=True)
                    self._recon = None
                    self._recon_endpoint = None

    def _start_loop_thread(self) -> None:
        """Start the coordinator loop unless one is already running."""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="ipc-competition", daemon=True
            )
            self._thread.start()

    def _supervise(self) -> None:
        """Revive the coordinator if its loop thread ever dies unexpectedly.

        A silent thread death strands every seat until an operator restarts
        the application, so the supervisor stays deliberately independent of
        the loop it watches and never clears ``_stop`` on its own.
        """
        while not self._stop.wait(30):
            if self._thread is not None and self._thread.is_alive():
                continue
            print(
                "[ipc-competition] coordinator thread is not alive; restarting",
                file=sys.stderr,
                flush=True,
            )
            try:
                self._start_loop_thread()
            except Exception as exc:
                _recovery_logger.debug('Error handled in _supervise', exc_info=True)
                print(
                    f"[ipc-competition] supervisor restart failed:"
                    f" {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def shutdown(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)
        if self._supervisor and self._supervisor.is_alive():
            self._supervisor.join(timeout=5)
        if self._pair is not None:
            self._pair.close()
            self._pair = None
        if self._recon is not None:
            self._recon.close()
            self._recon = None
            self._recon_endpoint = None
        for project_id, epoch in list(self._engine_epochs.items()):
            with suppress(Exception):
                self.store.release_engine_lease(
                    project_id, "competition", self.engine_owner, epoch=epoch
                )
            self._engine_epochs.pop(project_id, None)
        self._wp_pool.shutdown(wait=True, cancel_futures=True)
        self._digest_pool.shutdown(wait=False, cancel_futures=True)

    @property
    def recon_endpoint(self) -> str | None:
        """ZeroMQ endpoint for asynchronous same-challenge collaboration."""
        return self._recon_endpoint

    def publish_recon(self, message: Envelope) -> dict:
        """Persist then announce one collaboration event.

        All callers use this seam instead of sending directly on a PUB socket.
        On Windows or during a socket restart it intentionally falls back to a
        durable-only write; a later Linux subscriber catches the record up by
        sequence through PostgreSQL.
        """
        if self._recon is not None:
            return self._recon.publish(message)
        return self.store.persist_recon_message(message)

    def _execute_pair_message(self, message, cancel: threading.Event) -> dict:
        handler = getattr(self.state, "competition_message_handler", None)
        if handler is None:
            return {
                "status": "failed",
                "request_id": message.request_id,
                "error": "receiver session has no registered control handler",
            }
        return handler(message, cancel)

    def wake(self) -> None:
        self._wake.set()

    def _workflow(self, workflow_id: str) -> dict:
        """Load a workflow for preflight or Start without a confirmation gate.

        Start is authorised by the operator pressing Start in the web UI, so this
        path no longer demands a CONFIRM phrase.  The capability token stays
        available for the legacy single-shot execute API, which still verifies it.
        """
        return self.ops.store.get_workflow(workflow_id)

    def _platform_for_workflow(self, workflow: dict) -> CompetitionPlatform:
        if str(getattr(workflow["spec"].challenges, "platform", "")) == "local":
            raise ValueError("local challenges are started through a run, not a workflow")
        if self._platform_factory is not None:
            return self._platform_factory(workflow["id"], workflow["spec"])
        return CompetitionPlatform(self.ops, workflow["id"], workflow["spec"])

    def platform_for_workflow(self, workflow_id: str) -> CompetitionPlatform:
        """Build the live adapter for one workflow without creating a run."""
        return self._platform_for_workflow(self._workflow(workflow_id))

    def _platform_for_run(self, run: dict) -> CompetitionPlatform:
        snapshot = run["config_snapshot"]
        spec = PlatformWorkflowSpec.model_validate(snapshot["workflow_spec"])
        if str(spec.challenges.platform) == "local":
            return self._local_platform(run, snapshot)
        if self._platform_factory is not None:
            return self._platform_factory(run["workflow_id"], spec)
        return CompetitionPlatform(self.ops, run["workflow_id"], spec)

    def _local_project(self, project_id: str) -> dict:
        with self.state.db.connect() as connection:
            row = connection.execute(
                "SELECT id, title, category, status FROM projects WHERE id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            return {}
        return {
            "project_id": str(row["id"]),
            "title": str(row["title"] or row["id"]),
            "category": str(row["category"] or ""),
            "goal": "",
            "solved": str(row["status"] or "") in {"solved", "completed"},
        }

    def _local_platform(self, run: dict, snapshot: dict) -> LocalPlatform:
        project = snapshot.get("local_project")
        if not isinstance(project, dict) or not project.get("project_id"):
            challenges = self.store.challenges(run["id"])
            project_id = str(challenges[0]["project_id"] or "") if challenges else ""
            project = self._local_project(project_id)
        return LocalPlatform(project, self.state.db)

    def resume_local(self, project_id: str) -> dict | None:
        """Return the one-challenge local run that already owns this Project."""

        existing = self.store.run_by_idempotency(f"local:{project_id}")
        if existing is None:
            return None
        return self.start_local(project_id)

    def start_local(self, project_id: str, idempotency_key: str = "") -> dict:
        """Adopt one existing Project as a single-challenge local run.

        Local CTF has no second execution loop: the Project keeps its id,
        graph, attachments, writeups and history, while the run owns
        scheduling, submission and stop/resume semantics.
        """

        errors = self.state.config.startup_errors()
        if errors:
            raise ValueError("; ".join(errors))
        project = self._local_project(project_id)
        if not project:
            raise KeyError(project_id)
        key = idempotency_key.strip() or f"local:{project_id}"
        existing = self.store.run_by_idempotency(key)
        if existing is not None:
            if existing["status"] in {"stopped", "finished", "paused", "blocked"}:
                self.store.revive_run(existing["id"])
                self.store.append_run_event(
                    existing["id"], "run.resumed", {"mode": "local"}
                )
                self.wake()
                existing = self.store.get_run(existing["id"])
            return self.store.run_snapshot(existing["id"])
        spec = PlatformWorkflowSpec.model_validate(
            {
                "name": f"Local {project['title']}"[:120],
                "challenges": {"platform": "local"},
            }
        )
        workflow = self.ops.store.create_workflow(spec, source="local")
        snapshot = {
            "workflow_spec": spec.model_dump(mode="json"),
            "spec_digest": workflow["spec_digest"],
            "member_config": _safe_member_config(self.state.config.member),
            "member_config_source": "runtime-config",
            "limits_snapshot": {
                "max_concurrent_tasks": self.state.config.limits.max_concurrent_tasks,
                "total_cpu": getattr(self.state.config.limits, "total_cpu", 0),
                "remote_concurrency": 0,
                "remote_source": "local",
            },
            "limits": {},
            "local_project": project,
        }
        run = self.store.create_run(workflow["id"], f"local://{project_id}", key, snapshot)
        if run["status"] != "preflight":
            return self.store.run_snapshot(run["id"])
        self.store.append_run_event(
            run["id"], "run.created", {"workflow_id": workflow["id"], "mode": "local"}
        )
        run = self.store.transition_run(run["id"], "importing", revision=run["revision"])
        challenge = self.store.register_challenge(
            run["id"],
            project_id,
            {
                "title": project["title"],
                "category": project["category"],
                "platform": "local",
                "remote": False,
            },
        )
        self.store.link_project(challenge["id"], project_id)
        self.store.mark_ready(challenge["id"])
        run = self.store.transition_run(run["id"], "running", revision=run["revision"])
        self.store.append_run_event(
            run["id"], "run.started", {"challenge_count": 1, "mode": "local"}
        )
        self.wake()
        return self.store.run_snapshot(run["id"])

    def _import_for_run(
        self, workflow_id: str, spec: PlatformWorkflowSpec,
        external_ids: list[str], run_id: str,
    ) -> dict[str, Any]:
        """Import run-owned Projects while retaining deterministic test seams."""
        importer = self.ops._import
        try:
            accepts_run = "run_id" in inspect.signature(importer).parameters
        except (TypeError, ValueError):
            accepts_run = True
        if accepts_run:
            return importer(workflow_id, spec, external_ids, run_id=run_id)
        return importer(workflow_id, spec, external_ids)

    def preflight(self, workflow_id: str) -> dict:
        errors = self.state.config.startup_errors()
        if errors:
            raise ValueError("; ".join(errors))
        workflow = self._workflow(workflow_id)
        platform = self._platform_for_workflow(workflow)
        challenges = platform.preflight()
        supports_submit = getattr(
            platform,
            "supports_submit",
            workflow["spec"].submit is not None
            or getattr(platform, "client", None) is not None,
        )
        if callable(supports_submit):
            supports_submit = supports_submit()
        supports_instances = getattr(platform, "supports_instances", None)
        if supports_instances is None:
            supports_instances = getattr(platform, "client", None) is not None
        elif callable(supports_instances):
            supports_instances = supports_instances()
        preflight_remote_limit, remote_source = self._resolve_remote_limit(
            workflow["spec"].model_dump(mode="json"), platform,
        )
        return {
            "workflow_id": workflow_id,
            "identity_key": platform.identity,
            "network": platform.network_diagnostics() if callable(getattr(platform, "network_diagnostics", None)) else {},
            "challenge_count": len(challenges),
            "remote_count": sum(bool(item.remote) for item in challenges),
            "ordinary_count": sum(not bool(item.remote) for item in challenges),
            "challenges": [
                {
                    "external_id": str(item.external_id),
                    "title": item.title,
                    "category": item.category,
                    "remote": bool(item.remote),
                    "solved": bool(item.solved),
                }
                for item in challenges
            ],
            "limits": {
                "local_concurrency": self.state.config.limits.max_concurrent_tasks,
                "remote_concurrency": preflight_remote_limit,
                "source": remote_source,
            },
            "capabilities": {
                "submit": bool(supports_submit),
                "instances": bool(supports_instances),
                "async_verdict": bool(
                    workflow["spec"].submit and workflow["spec"].submit.pending_values
                ),
            },
        }

    def start(
        self,
        workflow_id: str,
        idempotency_key: str,
        select: list[str] | None = None,
    ) -> dict:
        errors = self.state.config.startup_errors()
        if errors:
            raise ValueError("; ".join(errors))
        workflow = self._workflow(workflow_id)
        platform = self._platform_for_workflow(workflow)
        supports_submit = getattr(platform, "supports_submit", False)
        if callable(supports_submit):
            supports_submit = supports_submit()
        if not bool(supports_submit):
            raise ValueError("platform adapter does not support flag submission")
        existing = self.store.run_by_idempotency(idempotency_key)
        if existing is not None:
            requested_identity = platform.identity
            stored_identity = str(existing["identity_key"])
            if existing["workflow_id"] != workflow_id or not (
                stored_identity == requested_identity
                or stored_identity.startswith(requested_identity + "/account/")
            ):
                raise CompetitionConflict("idempotency key belongs to another run request")
            return self.store.run_snapshot(existing["id"])
        discovered = _select_discovered(platform.preflight(), select)
        snapshot_limits = {
            "max_concurrent_tasks": self.state.config.limits.max_concurrent_tasks,
            "total_cpu": getattr(self.state.config.limits, "total_cpu", 0),
        }
        snapshot_limits["remote_concurrency"], snapshot_limits["remote_source"] = self._resolve_remote_limit(
            workflow["spec"].model_dump(mode="json"), platform,
        )
        snapshot = {
            "workflow_spec": workflow["spec"].model_dump(mode="json"),
            "spec_digest": workflow["spec_digest"],
            "member_config": _safe_member_config(self.state.config.member),
            "member_config_source": "runtime-config",
            "limits_snapshot": snapshot_limits,
            "limits": {},
        }
        if select is not None:
            snapshot["selected_external_ids"] = [
                str(item.external_id) for item in discovered
            ]
        run = self.store.create_run(
            workflow_id, platform.identity, idempotency_key, snapshot
        )
        if run["status"] != "preflight":
            return self.store.run_snapshot(run["id"])
        self.store.append_run_event(run["id"], "run.created", {"workflow_id": workflow_id})
        run = self.store.transition_run(run["id"], "importing", revision=run["revision"])
        try:
            imported = self._import_for_run(
                workflow_id,
                workflow["spec"],
                [item.external_id for item in discovered],
                run["id"],
            )["imported"]
            projects = {item["external_id"]: item["project_id"] for item in imported}
            for item in discovered:
                challenge = self.store.register_challenge(
                    run["id"], item.external_id, self._challenge_metadata(item)
                )
                self.store.link_project(challenge["id"], projects[item.external_id])
                if item.solved:
                    self.store.mark_external_solved(challenge["id"])
                    self._release_external_solved(run, challenge)
                    self.store.append_run_event(
                        run["id"], "challenge.external_solved",
                        {"challenge_id": challenge["id"], "external_id": item.external_id},
                    )
                else:
                    self.store.mark_ready(challenge["id"])
            run = self.store.transition_run(
                run["id"], "running", revision=run["revision"]
            )
            self.store.append_run_event(
                run["id"], "run.started", {"challenge_count": len(discovered)}
            )
            self._validate_run_window(run, platform)
            self.wake()
            return self.store.run_snapshot(run["id"])
        except Exception as exc:
            current = self.store.get_run(run["id"])
            if current["status"] == "importing":
                self.store.transition_run(
                    run["id"], "blocked", revision=current["revision"]
                )
            self.store.schedule_next_sync(run["id"], seconds=self.sync_interval, error=str(exc))
            self.store.append_run_event(
                run["id"], "run.blocked", {"reason": type(exc).__name__}
            )
            raise

    @staticmethod
    def _challenge_metadata(item) -> dict[str, Any]:
        platform_data = getattr(item, "platform_data", None) or {}
        solve_count = platform_data.get("solve_count")
        try:
            solve_count = int(solve_count) if solve_count is not None else None
        except (TypeError, ValueError):
            solve_count = None
        metadata: dict[str, Any] = {
            "title": item.title,
            "category": item.category,
            "description": item.description,
            "attachment_count": len(item.attachment_urls),
            "remote": bool(item.remote),
            "external_solved": bool(item.solved),
            "hints": list(item.hints),
            "ready_at": datetime.now(timezone.utc).isoformat(),
        }
        if solve_count is not None:
            metadata["solve_count"] = solve_count
        difficulty = str(platform_data.get("difficulty") or "").strip().lower()
        if difficulty:
            metadata["difficulty"] = difficulty
        container_type = platform_data.get("container_type")
        if container_type:
            metadata["container_type"] = str(container_type)
        try:
            score = float(platform_data.get("score"))
        except (TypeError, ValueError):
            score = None
        if score is not None and 0 <= score < float("inf"):
            metadata["score"] = score
        return metadata

    def control(self, run_id: str, action: str, revision: int, operation_id: str | None = None) -> dict:
        if not self._tick_lock.acquire(blocking=False):
            if not callable(getattr(self.store, "queue_control", None)):
                error = CompetitionConflict("another competition operation is in progress; retry after 20 seconds")
                error.retry_after = 20
                raise error
            operation = self.store.queue_control(run_id, action, revision, operation_id)
            self.wake()
            return {**self.store.run_snapshot(run_id), "control_operation": operation}
        try:
            if operation_id:
                operation = self.store.queue_control(run_id, action, revision, operation_id)
                self.wake()
                return {**self.store.run_snapshot(run_id), "control_operation": operation}
            return self._control_unlocked(run_id, action, revision)
        finally:
            self._tick_lock.release()

    def _control_unlocked(self, run_id: str, action: str, revision: int) -> dict:
        run = self.store.get_run(run_id)
        if run["revision"] != revision:
            raise CompetitionConflict("stale run revision")
        if action == "refresh":
            self._refresh_unlocked(run_id)
            return self.store.run_snapshot(run_id)
        target = {"pause": "paused", "resume": "running", "stop": "stopped"}.get(action)
        if target is None:
            raise ValueError("unsupported run action")
        if run["status"] != target:
            self.store.transition_run(run_id, target, revision=revision, permit_active=action == "stop")
        if action == "stop":
            self._teardown_stopped_run(run_id, run)
        elif action == "pause":
            self._stop_run_projects(run_id)
            self.store.release_run_assignments(run_id)
        self.store.append_run_event(run_id, f"run.{target}", {})
        if action == "resume":
            self.wake()
        return self.store.run_snapshot(run_id)

    def _process_controls(self) -> bool:
        if not callable(getattr(self.store, "claim_control", None)):
            return False
        processed = False
        for _ in range(10):
            operation = self.store.claim_control(self.owner)
            if operation is None:
                break
            error = None
            try:
                run = self.store.get_run(operation["run_id"])
                self._control_unlocked(run["id"], operation["action"], run["revision"])
            except Exception as exc:
                _recovery_logger.debug("Control operation failed", exc_info=True)
                error = f"{type(exc).__name__}: {exc}"[:2000]
            if self.store.finish_control(operation, error=error):
                self.store.append_run_event(operation["run_id"], "run.control_failed" if error else "run.control_completed",
                                            {"operation_id": operation["id"], "action": operation["action"], "error": error})
            processed = True
        return processed

    def update_limits(self, run_id: str, limits: dict, revision: int) -> dict:
        if not self._tick_lock.acquire(timeout=20):
            error = CompetitionConflict("another competition operation is in progress; retry after 20 seconds")
            error.retry_after = 20
            raise error
        try:
            updated = self.store.update_limits(run_id, limits, revision=revision)
            self.store.append_run_event(run_id, "run.limits_updated", {"limits": limits})
            self.wake()
            return self.store.run_snapshot(updated["id"])
        finally:
            self._tick_lock.release()


    @staticmethod
    def _is_platform_closed(exc: BaseException) -> bool:
        return str(getattr(exc, "reason", "") or "") in {"ended", "closed"}

    def _note_platform_closed(self, run_id: str, exc: BaseException) -> None:
        """Remember that the platform task is gone; tick() performs the teardown."""

        message = f"{type(exc).__name__}: {exc}"[:400]
        self._closed_runs.setdefault(str(run_id), message)

    def _flush_platform_closed(self) -> None:
        """Finish every run whose platform task has ended."""

        if not self._closed_runs:
            return
        pending = dict(self._closed_runs)
        self._closed_runs.clear()
        for run_id, message in pending.items():
            try:
                run = self.store.get_run(run_id)
            except KeyError:
                continue
            if run["status"] in {"finished", "stopped"}:
                continue
            try:
                self._close_run(run, reason="platform_closed", error=message)
            except Exception as exc:
                _recovery_logger.debug("Error handled in _flush_platform_closed", exc_info=True)
                with suppress(Exception):
                    self.store.append_run_event(
                        run_id, "run.close_failed",
                        {"error": f"{type(exc).__name__}: {exc}"[:400]},
                    )
                self._closed_runs.setdefault(run_id, message)

    def _close_run(self, run: dict, *, reason: str, error: str = "") -> None:
        """Drain and finish a run the platform has already closed.

        Deliberately the same three steps as an operator stop, in the same
        order, so a seat or a container can never be stranded by a platform
        timeout we did not initiate.
        """

        run_id = run["id"]
        self.store.append_run_event(
            run_id, "run.platform_closed", {"reason": reason, "error": error},
        )
        current = self.store.get_run(run_id)
        if current["status"] in {"running", "paused"}:
            current = self.store.transition_run(
                run_id, "draining", revision=current["revision"],
            )
        elif current["status"] in {"preflight", "importing", "blocked"}:
            current = self.store.transition_run(
                run_id, "stopped", revision=current["revision"], permit_active=True,
            )
            self._teardown_stopped_run(run_id, current)
            self.store.append_run_event(run_id, "run.stopped", {"reason": reason})
            return
        for challenge in self.store.challenges(run_id):
            if challenge["state"] not in {"solved", "expired", "cancelled", "withdrawn"}:
                self.store.set_challenge_state(challenge["id"], "expired")
        self._teardown_stopped_run(run_id, current)
        current = self.store.get_run(run_id)
        if current["status"] == "draining":
            self.store.transition_run(
                run_id, "finished", revision=current["revision"], permit_active=True,
            )
        self.store.append_run_event(run_id, "run.finished", {"reason": reason})
        self._write_run_summary(self.store.get_run(run_id))

    def _teardown_stopped_run(self, run_id: str, run: dict) -> None:
        """Tear a stopped run down so no step's failure strands a seat (T4).

        The revision transition has already landed, so the run *is* stopped the
        instant this returns regardless of what happens here. The danger the old
        ordering carried was that a raising ``_stop_owned_instances`` skipped the
        ``run.stopped`` event and left assignments live, so the orchestrator's
        recovery loop later resurrected the seat. Every step here is therefore
        best-effort and isolated: a failure in one is recorded and the rest
        still run, and releasing the seats/leases is attempted before the slow,
        failure-prone container teardown so a container error can never leave a
        lease holding a seat the scheduler would reclaim.
        """
        steps = (
            ("stop_projects", lambda: self._stop_run_projects(run_id)),
            ("release_assignments", lambda: self.store.release_run_assignments(run_id)),
            ("stop_instances", lambda: self._stop_owned_instances(run)),
        )
        for label, step in steps:
            try:
                self._run_teardown_step(step)
            except Exception as exc:
                _recovery_logger.debug('Error handled in _teardown_stopped_run', exc_info=True)
                self.state.logger.project(
                    "competition_teardown_step_failed", run_id,
                    step=label, error=f"{type(exc).__name__}: {exc}",
                )
                with suppress(Exception):
                    self.store.append_run_event(
                        run_id, "run.teardown_failed",
                        {"step": label, "error": f"{type(exc).__name__}: {exc}"},
                    )

    @staticmethod
    def _run_teardown_step(step, *, attempts: int = 3, base_delay: float = 0.2) -> None:
        """Run one teardown step with a bounded retry on a transient failure.

        Teardown races the very conditions that make it necessary (a dying
        engine, a container mid-removal), so a first attempt can fail on a
        transient error that a short retry clears. The budget is small and
        bounded: a step that still fails after it propagates to the caller,
        which records it and moves on rather than looping forever.
        """
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                step()
                return
            except Exception as exc:
                _recovery_logger.debug('Error handled in _run_teardown_step', exc_info=True)
                last = exc
                if attempt >= attempts:
                    break
                time.sleep(base_delay * attempt)
        if last is not None:
            raise last

    def _stop_run_projects(self, run_id: str) -> None:
        orchestrator = self.state.orchestrator
        for challenge in self.store.challenges(run_id):
            if challenge.get("project_id"):
                if orchestrator is not None:
                    with suppress(Exception):
                        orchestrator.stop_project(challenge["project_id"])
                self._release_project_engine(challenge["project_id"])
        self._resolve_orphaned_jobs(run_id)

    def _resolve_orphaned_jobs(self, run_id: str) -> None:
        """Mark detached jobs 'lost' once their container is known to be gone.

        Deliberately ``lost`` and not ``exited``: the exit status was never
        recorded, so claiming one here would invent evidence. A reader must be
        able to tell "we do not know how this ended" from "it succeeded".
        """
        from backend.agent.jobs import AgentJobStore

        jobs = AgentJobStore(self.state.db)
        for assignment in self.store.assignments(run_id):
            with suppress(Exception):
                for job in jobs.mark_lost_for_session(assignment["session_id"]):
                    self.store.append_run_event(
                        run_id, "job.lost",
                        {
                            "job_id": job["id"],
                            "session_id": job["session_id"],
                            "command": job["command"][:500],
                        },
                    )

    def _sweep_stale_jobs(self, run: dict) -> None:
        """Periodically close jobs whose process went silent (T5).

        ``_resolve_orphaned_jobs`` only fires on an explicit stop and only for a
        run's own seats.  A background job can also be orphaned mid-run -- its
        container removed, its process killed -- leaving a ``running`` row no
        poller will ever close.  This time-based sweep closes any job idle past
        the threshold as ``lost`` and records it once as ``job.swept_lost``.
        """
        from backend.agent.jobs import AgentJobStore

        try:
            jobs = AgentJobStore(self.state.db)
            swept = jobs.sweep_stale(idle_seconds=self._stale_job_seconds)
        except Exception as exc:
            _recovery_logger.debug('Error handled in _sweep_stale_jobs', exc_info=True)
            self.state.logger.project(
                "competition_job_sweep_failed", run["id"],
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        for job in swept:
            self.store.append_run_event(
                run["id"], "job.swept_lost",
                {
                    "job_id": job["id"],
                    "session_id": job["session_id"],
                    "command": (job.get("command") or "")[:500],
                    "idle_seconds": self._stale_job_seconds,
                },
            )

    def _claim_project_engine(self, run: dict, project_id: str) -> bool:
        try:
            lease = self.store.claim_engine_lease(
                project_id, "competition", self.engine_owner, seconds=60
            )
        except (UndefinedColumn, UndefinedTable):
            return True
        except Exception as exc:
            _recovery_logger.debug('Error handled in _claim_project_engine', exc_info=True)
            self.store.append_run_event(
                run["id"], "engine.lease_error",
                {"project_id": project_id, "error": str(exc)},
            )
            return False
        if lease is not None:
            self._engine_epochs[project_id] = int(lease["engine_epoch"])
            self._engine_conflicts.discard((run["id"], project_id))
            return True
        key = (run["id"], project_id)
        if key not in self._engine_conflicts:
            self._engine_conflicts.add(key)
            self.store.append_run_event(
                run["id"], "engine.conflict",
                {
                    "project_id": project_id,
                    "requested": "competition",
                    "reason": "another scheduler owns the project",
                },
            )
        return False

    def _renew_project_engines(self, run: dict) -> None:
        for challenge in self.store.challenges(run["id"]):
            project_id = challenge.get("project_id")
            if not project_id:
                continue
            epoch = self._engine_epochs.get(project_id)
            if epoch is None:
                self._claim_project_engine(run, project_id)
                continue
            try:
                renewed = self.store.renew_engine_lease(
                    project_id, "competition", self.engine_owner,
                    epoch=epoch, seconds=60
                )
            except Exception:
                _recovery_logger.debug('Error handled in _renew_project_engines', exc_info=True)
                renewed = True
            if not renewed:
                self._claim_project_engine(run, project_id)

    def _release_project_engine(self, project_id: str) -> None:
        epoch = self._engine_epochs.pop(project_id, None)
        if epoch is None:
            return
        with suppress(Exception):
            self.store.release_engine_lease(
                project_id, "competition", self.engine_owner,
                epoch=epoch,
            )

    def _stop_owned_instances(self, run: dict) -> None:
        """Ask the platform to close every owned container without blocking the tick.

        The platform call is the slow half of a slot handoff; submitting it to
        the bounded stop pool lets the same tick start the next challenge while
        the old container is going away. Failures are recorded as
        stop_pending and retried by _retry_pending_instance_stops.
        """

        platform = self._platform_for_run(run)
        with self.state.db.connect() as connection:
            instances = connection.execute(
                """SELECT * FROM competition_instances
                   WHERE run_id=%s AND owned=true AND state NOT IN ('stopped','expired')""",
                (run["id"],),
            ).fetchall()
        futures = []
        for instance in instances:
            challenge = {
                "id": instance["challenge_id"], "external_id": instance["external_id"],
            }
            with suppress(Exception), self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances
                       SET state='stop_pending',updated_at=now()
                       WHERE challenge_id=%s AND state NOT IN ('stopped','expired')""",
                    (challenge["id"],),
                )
            try:
                futures.append(
                    self._stop_pool.submit(
                        self._stop_instance, run, challenge, platform=platform,
                    )
                )
            except RuntimeError:
                self._stop_instance(run, challenge, platform=platform)
        deadline = time.monotonic() + 60
        for future in futures:
            with suppress(Exception):
                future.result(timeout=max(1.0, deadline - time.monotonic()))

    def _submit_stop(self, run: dict, challenge: dict, *, platform=None) -> bool:
        """Mark the slot as closing now and stop it on the bounded pool."""

        platform = platform or self._platform_for_run(run)
        with suppress(Exception), self.state.db.connect() as connection:
            connection.execute(
                """UPDATE competition_instances
                   SET state='stop_pending',updated_at=now()
                   WHERE challenge_id=%s AND state NOT IN ('stopped','expired')""",
                (challenge["id"],),
            )
        try:
            self._stop_pool.submit(self._stop_instance, run, challenge, platform=platform)
            return True
        except RuntimeError:
            return self._stop_instance(run, challenge, platform=platform)

    def _stop_instance(self, run: dict, challenge: dict, *, platform=None) -> bool:
        platform = platform or self._platform_for_run(run)
        self.store.append_run_event(run["id"], "instance.stop_requested", {"challenge_id": challenge["id"]})
        started = time.monotonic()
        try:
            platform.stop_instance(challenge["external_id"])
        except Exception as exc:
            _recovery_logger.debug('Error handled in _stop_instance', exc_info=True)
            if self._is_platform_closed(exc):
                self._note_platform_closed(run["id"], exc)
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances
                       SET state='stop_pending',metadata=metadata || %s,updated_at=now()
                       WHERE challenge_id=%s""",
                    (Jsonb({"stop_error": str(exc)}), challenge["id"]),
                )
            self.store.append_run_event(
                run["id"], "instance.stop_failed",
                {"challenge_id": challenge["id"], "error": str(exc)},
            )
            return False
        stop_ms = int((time.monotonic() - started) * 1000)
        with self.state.db.connect() as connection:
            connection.execute(
                """UPDATE competition_instances SET state='stopped',updated_at=now()
                   WHERE challenge_id=%s""",
                (challenge["id"],),
            )
        self._slot_free_at[challenge["id"]] = time.monotonic()
        self.store.append_run_event(
            run["id"], "instance.stopped",
            {"challenge_id": challenge["id"], "stop_ms": stop_ms},
        )
        return True

    def _prestart_lookahead(self, run: dict) -> None:
        """Warm the next remote container while the current slot is closing.

        The platform API is not the bottleneck (launch p90 was 4s); the wasted
        time is the gap between closing a slot and the next assignment starting
        its container. Two lookahead workers open the next instance early,
        without assigning it, and only while the platform quota has room.
        """

        orchestrator = getattr(self.state, "orchestrator", None)
        stop_event = getattr(self, "_stop", None)
        if orchestrator is None or (stop_event is not None and stop_event.is_set()):
            return
        if run.get("status") not in {None, "running"}:
            return
        platform = self._platform_for_run(run)
        if not self._supports_instance_lifecycle(platform):
            return
        for key, future in list(self._prestart_futures.items()):
            if future.done():
                self._prestart_futures.pop(key, None)
        rows = self.store.challenges(run["id"])
        active = self.store.assignments(run["id"], active_only=True)
        challenges = self._policy_challenges(rows, active)
        remote = [
            challenge for challenge in challenges
            if challenge.remote and challenge.state in {"ready", "solving"}
            and (challenge.retry_at is None or challenge.retry_at <= datetime.now(timezone.utc))
        ]
        if not remote:
            return
        remote_limit = self._remote_instance_limit(run, platform)
        remote_used = sum(bool(item.members) and item.remote for item in challenges)
        with suppress(Exception):
            with self.state.db.connect() as connection:
                row = connection.execute(
                    """SELECT COUNT(*) AS active FROM competition_instances i
                       WHERE i.run_id=%s
                         AND i.state IN ('ready','running','active','start_pending','stop_pending')
                         AND NOT EXISTS (
                           SELECT 1 FROM competition_assignments a
                           WHERE a.challenge_id=i.challenge_id AND a.released_at IS NULL)""",
                    (run["id"],),
                ).fetchone()
            remote_used += int((row or {}).get("active") or 0)
        free = max(0, remote_limit - remote_used)
        if free <= 0:
            return
        now = datetime.now(timezone.utc)
        ends_at = self._run_ends_at(run)
        started_at = _timestamp(run.get("created_at"))
        plans = plan_assignments(
            remote,
            occupied_members={item["member"] for item in active},
            remote_capacity=free,
            now=now,
            local_capacity=0,
            available_members={member.name for member in self.state.config.available_members()},
            phase=phase_for(now, started_at, ends_at),
            remaining_seconds=(ends_at - now).total_seconds() if ends_at else None,
        )
        by_id = {item["id"]: item for item in rows}
        blocked: set[str] = set()
        with suppress(Exception), self.state.db.connect() as connection:
            pending_rows = connection.execute(
                """SELECT challenge_id,state,metadata FROM competition_instances
                   WHERE run_id=%s AND state IN ('expired','start_pending','stop_pending')""",
                (run["id"],),
            ).fetchall()
        for item in pending_rows:
            if item["state"] == "expired":
                repair = _timestamp((item.get("metadata") or {}).get("repair_after"))
                if repair is None or repair > now:
                    blocked.add(item["challenge_id"])
            else:
                blocked.add(item["challenge_id"])
        scheduled = 0
        for plan in plans:
            if not plan.needs_instance or scheduled >= 2:
                continue
            if plan.challenge_id in blocked:
                continue
            key = (run["id"], plan.challenge_id)
            if key in self._prestart_futures:
                continue
            if self._instance_ready(plan.challenge_id):
                continue
            row = by_id.get(plan.challenge_id)
            if row is None:
                continue
            try:
                self._prestart_futures[key] = self._prestart_pool.submit(
                    self._start_instance, run, row
                )
            except RuntimeError:
                return
            scheduled += 1
            self.store.append_run_event(
                run["id"], "instance.prestart",
                {"challenge_id": plan.challenge_id, "member": plan.member},
            )

    def _retry_pending_instance_stops(self) -> None:
        with self.state.db.connect() as connection:
            rows = connection.execute(
                """SELECT i.challenge_id,i.external_id,i.run_id,r.workflow_id
                   FROM competition_instances i
                   JOIN competition_runs r ON r.id=i.run_id
                   WHERE i.state='stop_pending'
                     AND i.updated_at <= now() - interval '60 seconds'
                   ORDER BY i.updated_at LIMIT 20"""
            ).fetchall()
        for row in rows:
            try:
                run = self.store.get_run(row["run_id"])
                self._stop_instance(
                    run,
                    {"id": row["challenge_id"], "external_id": row["external_id"]},
                )
            except Exception as exc:
                _recovery_logger.debug('Error handled in _retry_pending_instance_stops', exc_info=True)
                self.store.append_run_event(
                    row["run_id"], "instance.stop_retry_failed",
                    {"challenge_id": row["challenge_id"], "error": str(exc)},
                )

    def refresh(self, run_id: str) -> dict:
        if not self._tick_lock.acquire(blocking=False):
            raise CompetitionConflict("another competition operation is in progress")
        try:
            return self._refresh_unlocked(run_id)
        finally:
            self._tick_lock.release()

    def _refresh_unlocked(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        platform = self._platform_for_run(run)
        try:
            discovered = platform.challenges()
        except Exception as exc:
            if self._is_platform_closed(exc):
                self._note_platform_closed(run_id, exc)
            raise
        selected = _selected_external_ids(run)
        if selected is not None:
            discovered = [
                item for item in discovered if item.external_id in selected
            ]
        known = {item["external_id"]: item for item in self.store.challenges(run_id)}
        new_items = [item for item in discovered if item.external_id not in known]
        projects: dict[str, str] = {}
        if new_items:
            imported = self._import_for_run(
                run["workflow_id"], platform.spec,
                [item.external_id for item in new_items],
                run["id"],
            )["imported"]
            projects = {item["external_id"]: item["project_id"] for item in imported}
        for item in discovered:
            challenge = self.store.register_challenge(
                run_id, item.external_id, self._challenge_metadata(item)
            )
            if item.external_id in projects:
                self.store.link_project(challenge["id"], projects[item.external_id])
            if item.solved:
                self.store.mark_external_solved(challenge["id"])
                self._release_external_solved(run, challenge)
            elif item.external_id in projects:
                self.store.mark_ready(challenge["id"])
        discovered_ids = {item.external_id for item in discovered}
        for external_id, challenge in known.items():
            if external_id not in discovered_ids and challenge["state"] not in {
                "solved", "expired", "cancelled"
            }:
                self.store.set_challenge_state(challenge["id"], "withdrawn")
        self.store.schedule_next_sync(run_id, seconds=self.sync_interval)
        self.store.append_run_event(
            run_id, "run.synchronized",
            {"challenge_count": len(discovered), "new_count": len(new_items)},
        )
        return {"challenge_count": len(discovered), "new_count": len(new_items)}

    def submit_candidate(
        self, run_id: str, challenge_id: str, session_id: str,
        flag: str, evidence: str, generation: int,
    ) -> dict:
        assignment = next(
            (
                item for item in self.store.assignments(run_id, active_only=True)
                if item["challenge_id"] == challenge_id
                and item["session_id"] == session_id
                and item["role"] != "wp"
            ),
            None,
        )
        if assignment is None:
            raise CompetitionConflict("session has no active assignment for this challenge")
        queued = self.store.queue_candidate(
            assignment, flag, evidence, generation
        )
        self.store.append_run_event(
            run_id, "submission.queued",
            {"challenge_id": challenge_id, "submission_id": queued["id"]},
        )
        self.wake()
        return queued

    def retry_wp(self, job_id: str) -> dict:
        with self.state.db.connect() as connection:
            row = connection.execute(
                """UPDATE competition_wp_jobs SET status='pending',next_attempt_at=now(),last_error=NULL
                   WHERE challenge_id=%s AND (status='deferred' OR (status='failed' AND attempts<5)) RETURNING *""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise CompetitionConflict("WP job is not retryable")
        self.wake()
        return row

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except BaseException as exc:
                _recovery_logger.debug('Error handled in _run_loop', exc_info=True)
                try:
                    run = self.store.active_run()
                except Exception:
                    _recovery_logger.debug('Error handled in _run_loop', exc_info=True)
                    run = None
                if run:
                    with suppress(Exception):
                        self.store.schedule_next_sync(
                            run["id"], seconds=self.sync_interval, error=str(exc)
                        )
                        self.store.append_run_event(
                            run["id"], "coordinator.error",
                            {"error": f"{type(exc).__name__}: {exc}"},
                        )
                print(
                    f"[ipc-competition] tick failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            self._wake.wait(self.tick_interval)
            self._wake.clear()

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._process_controls()
            self._retry_pending_instance_stops()
            run = self.store.active_run()
            if run is None or not self.store.claim_run_lease(run["id"], self.owner):
                return
            if run["status"] == "blocked":
                due = _timestamp(run.get("next_sync_at"))
                if due is None or due <= datetime.now(timezone.utc):
                    try:
                        self._platform_for_run(run).preflight()
                        current = self.store.get_run(run["id"])
                        resumed = self.store.transition_run(
                            run["id"], "running", revision=current["revision"]
                        )
                        self.store.append_run_event(
                            run["id"], "run.recovered",
                            {"reason": "startup lease takeover and preflight"},
                        )
                        run = resumed
                    except Exception as exc:
                        _recovery_logger.debug('Error handled in tick', exc_info=True)
                        self.store.schedule_next_sync(run["id"], seconds=self.sync_interval, error=str(exc))
                        self.store.append_run_event(
                            run["id"], "run.recovery_failed", {"error": str(exc)}
                        )
                        return
            if run["status"] in {"running", "paused", "draining"}:
                self.store.heartbeat_run_assignments(run["id"], self.owner)
            self._flush_platform_closed()
            run = self.store.get_run(run["id"])
            if run["status"] in {"finished", "stopped"}:
                return
            if run["status"] in {"running", "paused"}:
                due = _timestamp(run.get("next_sync_at"))
                if due is None or due <= datetime.now(timezone.utc):
                    try:
                        self._refresh_unlocked(run["id"])
                    except Exception as exc:
                        _recovery_logger.debug('Error handled in tick', exc_info=True)
                        self.store.schedule_next_sync(
                            run["id"], seconds=self.sync_interval, error=str(exc)
                        )
                        self.store.append_run_event(
                            run["id"], "run.sync_failed", {"error": str(exc)}
                        )
            if run["status"] not in {"running", "paused", "draining"}:
                return
            self._enforce_deadline(run)
            run = self.store.get_run(run["id"])
            if run["status"] in {"finished", "stopped"}:
                return
            self._expire_at_competition_end(run)
            recovered = self.store.recover_expired_assignments(run["id"], self.owner)
            for assignment in recovered:
                self.store.append_run_event(
                    run["id"], "assignment.recovered",
                    {
                        "assignment_id": assignment["id"],
                        "challenge_id": assignment["challenge_id"],
                        "member": assignment["member"],
                        "epoch": assignment["epoch"],
                    },
                )
            self.store.release_expired_assignments(run["id"])
            self.store.reconcile_inflight_submissions(run["id"])
            self._stop_expired_challenges(run)
            self.store.heartbeat_run_assignments(run["id"], self.owner)
            self._report_stalled_assignments(run)
            self._check_message_delivery(run)
            self._sweep_stale_jobs(run)
            self._renew_project_engines(run)
            if self._process_controls():
                return
            if run["status"] == "running":
                self._sync_orchestrator_assignments(run)
            self._phase("instance_renewal", self._renew_instances, run)
            self._phase("instance_repair", self._repair_assigned_instances, run)
            self._publish_instance_entries(run)
            if self._process_controls():
                return
            self._phase("submissions", self._process_submissions,
                run,
                allow_new=run["status"] == "running",
            )
            self._advance_run_completion(run)
            current = self.store.get_run(run["id"])
            self._dispatch_wp(current)
            if current["status"] == "running":
                self._schedule_projects(run)
            self._record_observation(current)
        finally:
            self._tick_lock.release()

    def _report_stalled_assignments(self, run: dict) -> None:
        """Grade every live seat and act on the ones that stopped progressing.

        Two distinct signals feed this:

        * ``stalled_run_assignments`` -- the worker's own lease lapsed, which
          ``heartbeat_run_assignments`` already declined to renew. Recording it
          turns an otherwise unexplained expiry into a diagnosable event.
        * the health view -- graded quiet time, which can escalate to
          cancelling a turn or handing the session to another Member.

        Everything here is best-effort: observability and recovery must never
        interrupt judging or release a live lease, so failures are logged and
        the next tick retries.
        """
        try:
            stalled = self.store.stalled_run_assignments(run["id"])
        except Exception as exc:
            _recovery_logger.debug('Error handled in _report_stalled_assignments', exc_info=True)
            self.state.logger.project(
                "competition_stall_probe_failed", run["id"],
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        for assignment in stalled:
            if assignment["id"] in self._reported_stalls:
                continue
            self._reported_stalls.add(assignment["id"])
            self.store.append_run_event(
                run["id"], "assignment.stalled",
                {
                    "assignment_id": assignment["id"],
                    "challenge_id": assignment["challenge_id"],
                    "member": assignment["member"],
                    "role": assignment["role"],
                    "last_renewed_at": str(assignment.get("last_renewed_at")),
                },
            )
        live = {assignment["id"] for assignment in stalled}
        self._reported_stalls &= live
        self._run_session_watchdog(run)

    def _check_message_delivery(self, run: dict) -> None:
        """Fallback for recon that a live receiver session never consumed.

        建議3 (附属保险): the trunk fix is giving every seat an intent so its
        session actually runs and drains recon.  This is the safety net for the
        residual case where a session is live yet its message cursor stays put:
        a teammate's finding would otherwise sit in ``competition_messages``
        forever, invisible to the model.

        When a message addressed to a live seat is older than the grace window
        and still ahead of that seat's cursor, its payload is degraded into
        durable blackboard facts/hints on the receiver's project -- the same
        surface the model reads at the start of every turn -- and a
        ``competition.message_undelivered`` run event is written.  When a
        receiver's cursor later advances past a message we worried about, a
        ``competition.message_consumed`` metric event records that recovery.

        Best-effort throughout: a probe or injection failure is logged and the
        next tick retries; it must never interrupt judging or seat leases.
        """
        try:
            stranded = self.store.undelivered_messages(
                run["id"], older_than_seconds=self._message_delivery_window
            )
        except Exception as exc:
            _recovery_logger.debug('Error handled in _check_message_delivery', exc_info=True)
            self.state.logger.project(
                "competition_message_probe_failed", run["id"],
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        for message in stranded:
            sequence = int(message["sequence"])
            session_id = message.get("receiver_session_id")
            if session_id and session_id not in self._receiver_cursors:
                self._receiver_cursors[session_id] = self._cursor_or_zero(session_id)
            if sequence in self._undelivered_injected:
                continue
            if self._degrade_recon_to_blackboard(run, message):
                self._undelivered_injected.add(sequence)
        live_sequences = {int(message["sequence"]) for message in stranded}
        for session_id in list(self._receiver_cursors):
            self._note_cursor(run, session_id)
        self._undelivered_injected &= live_sequences

    def _cursor_or_zero(self, session_id: str) -> int:
        try:
            return self.store.message_cursor(session_id)
        except Exception:
            _recovery_logger.debug('Error handled in _cursor_or_zero', exc_info=True)
            return 0

    def _note_cursor(self, run: dict, session_id: str) -> None:
        """Emit competition.message_consumed when a receiver cursor advances."""
        try:
            current = self.store.message_cursor(session_id)
        except Exception:
            _recovery_logger.debug('Error handled in _note_cursor', exc_info=True)
            return
        previous = self._receiver_cursors.get(session_id, 0)
        if current > previous:
            self.store.append_run_event(
                run["id"], "competition.message_consumed",
                {"session_id": session_id, "cursor": current, "previous": previous},
            )
        self._receiver_cursors[session_id] = current

    def _degrade_recon_to_blackboard(self, run: dict, message: dict) -> bool:
        """Inject a stranded recon payload into the receiver's blackboard.

        Returns True once the payload is durably recorded (or there is nothing
        actionable to record) so the caller stops retrying it; returns False on
        a transient failure so the next tick tries again.
        """
        from backend.blackboard import graph_store, node_store

        envelope = message.get("envelope") or {}
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            payload = {}
        report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
        receiver = message.get("receiver_member") or envelope.get("receiver_member_id") or "?"
        sender = envelope.get("sender_member_id") or report.get("member") or "teammate"
        challenge_id = message.get("challenge_id")
        try:
            with self.state.db.connect() as connection:
                row = connection.execute(
                    "SELECT project_id FROM competition_challenges WHERE id=%s",
                    (challenge_id,),
                ).fetchone()
                project_id = row["project_id"] if row else None
                if not project_id:
                    return True
                knowledge = [
                    str(item).strip()
                    for item in (report.get("knowledge") or [])
                    if str(item).strip()
                ]
                directions = [
                    str(item).strip()
                    for item in (report.get("directions") or [])
                    if str(item).strip()
                ]
                progress = str(report.get("progress") or "").strip()
                creator = f"recon:{sender}"
                recorded = False
                for item in knowledge[:8]:
                    graph_store.create_hint(
                        connection, project_id,
                        f"[teammate {sender}] {item}", creator,
                    )
                    recorded = True
                for item in directions[:4]:
                    graph_store.create_hint(
                        connection, project_id,
                        f"[teammate {sender} suggests] {item}", creator,
                    )
                    recorded = True
                if progress:
                    node_store.create_fact(
                        connection, project_id,
                        f"Undelivered teammate recon from {sender}: {progress}"[:4000],
                    )
                    recorded = True
                if not recorded:
                    return True
        except Exception as exc:
            _recovery_logger.debug('Error handled in _degrade_recon_to_blackboard', exc_info=True)
            self.state.logger.project(
                "competition_message_degrade_failed", run["id"],
                error=f"{type(exc).__name__}: {exc}", challenge_id=str(challenge_id),
            )
            return False
        self.store.append_run_event(
            run["id"], "competition.message_undelivered",
            {
                "sequence": int(message["sequence"]),
                "challenge_id": challenge_id,
                "receiver": receiver,
                "sender": sender,
                "session_id": message.get("receiver_session_id"),
                "age_seconds": self._message_delivery_window,
                "injected_hints": True,
            },
        )
        return True

    def _run_session_watchdog(self, run: dict) -> None:
        """Escalate quiet sessions: diagnose, then cancel, then hand off."""
        from backend.agent.watchdog import classify, diagnosis

        try:
            rows = self.store.run_session_health(run["id"])
        except Exception as exc:
            _recovery_logger.debug('Error handled in _run_session_watchdog', exc_info=True)
            self.state.logger.project(
                "competition_health_probe_failed", run["id"],
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        for row in rows:
            if row.get("role") == "wp":
                continue
            verdict = classify(row)
            if not verdict.actionable:
                continue
            key = (row["assignment_id"], verdict.level)
            if key in self._watchdog_actions:
                continue
            self._watchdog_actions.add(key)
            self.store.append_run_event(
                run["id"], f"session.{verdict.level}",
                {
                    **diagnosis(row),
                    "assignment_id": row["assignment_id"],
                    "member": row.get("member"),
                    "challenge_id": row.get("challenge_id"),
                    "reason": verdict.reason,
                },
            )
            if verdict.level == "cancel":
                self._cancel_stalled_member(run, row)
            elif verdict.level == "handoff":
                self._handoff_stalled_seat(run, row)
        current = {row["assignment_id"] for row in rows}
        self._watchdog_actions = {
            item for item in self._watchdog_actions if item[0] in current
        }

    def _cancel_stalled_member(self, run: dict, row: dict) -> None:
        """Stop the in-flight turn, leaving the seat and transcript in place.

        The gentlest escalation that can actually change anything: the Member
        keeps its session and its findings and starts a fresh turn. If it was
        wedged in one provider call or tool, this is enough.
        """
        member = row.get("member")
        orchestrator = getattr(self.state, "orchestrator", None)
        stop = getattr(orchestrator, "stop_member", None) if orchestrator else None
        if not callable(stop) or not member:
            return
        try:
            stop(member)
        except Exception as exc:
            _recovery_logger.debug('Error handled in _cancel_stalled_member', exc_info=True)
            self.state.logger.project(
                "competition_turn_cancel_failed", run["id"],
                member=member, error=f"{type(exc).__name__}: {exc}",
            )

    def _handoff_stalled_seat(self, run: dict, row: dict) -> None:
        """Move the seat to an idle Member, keeping the session id.

        Keeping the session is the entire point: the replacement inherits every
        finding the stalled Member recorded instead of restarting the challenge
        from nothing.

        With no idle Member there is still one escalation left: rebuild the
        seat in place under a new epoch. Previously this branch only wrote
        ``session.handoff_skipped`` and the seat sat untouched — the last
        competition had stalled seats that never moved again.
        """
        replacement = self._idle_member(run)
        if replacement is None:
            self.store.append_run_event(
                run["id"], "session.handoff_skipped",
                {
                    "assignment_id": row["assignment_id"],
                    "reason": "no idle Member available to take the seat",
                },
            )
            self._rebuild_stalled_seat(run, row)
            return
        try:
            moved = self.store.handoff_assignment(
                row["assignment_id"], replacement, self.owner
            )
        except (CompetitionConflict, ValueError) as exc:
            self.store.append_run_event(
                run["id"], "session.handoff_failed",
                {"assignment_id": row["assignment_id"], "error": str(exc)},
            )
            return
        self.store.append_run_event(
            run["id"], "session.handed_off",
            {
                "assignment_id": moved["id"],
                "session_id": moved["session_id"],
                "previous_member": row.get("member"),
                "member": moved["member"],
                "epoch": moved["epoch"],
            },
        )
        self._restart_seat_project(run, moved)

    def _rebuild_stalled_seat(self, run: dict, row: dict) -> None:
        """Re-seat the same Member under a new epoch and restart its project.

        The last resort when nobody else is free. Advancing the epoch fences
        out whatever the old worker was still doing and lets a fresh one claim
        the same session, so the transcript and every recorded finding survive.
        """
        member = row.get("member")
        if not member:
            return
        try:
            rebuilt = self.store.handoff_assignment(
                row["assignment_id"], member, self.owner, require_stalled=False,
            )
        except (CompetitionConflict, ValueError) as exc:
            self.store.append_run_event(
                run["id"], "session.rebuild_failed",
                {"assignment_id": row["assignment_id"], "error": str(exc)},
            )
            return
        self.store.append_run_event(
            run["id"], "session.rebuilt",
            {
                "assignment_id": rebuilt["id"],
                "session_id": rebuilt["session_id"],
                "member": rebuilt["member"],
                "epoch": rebuilt["epoch"],
                "reason": "stalled_seat_no_replacement",
            },
        )
        self._restart_seat_project(run, rebuilt)

    def _restart_seat_project(self, run: dict, assignment: dict) -> None:
        """Ask the orchestrator to launch work for a seat that just moved.

        A new epoch alone does not start anything: the recovery only completes
        when the project is dispatched again, which is what turns a seat the
        watchdog repaired into a Member that is actually running.
        """
        orchestrator = getattr(self.state, "orchestrator", None)
        start = getattr(orchestrator, "start_project_async", None) if orchestrator else None
        if not callable(start):
            return
        try:
            with self.state.db.connect() as connection:
                challenge = connection.execute(
                    "SELECT project_id FROM competition_challenges WHERE id=%s",
                    (assignment["challenge_id"],),
                ).fetchone()
        except Exception:
            _recovery_logger.debug('Error handled in _restart_seat_project', exc_info=True)
            return
        project_id = challenge["project_id"] if challenge else None
        if not project_id:
            return
        try:
            start(project_id)
        except Exception as exc:
            _recovery_logger.debug('Error handled in _restart_seat_project', exc_info=True)
            self.store.append_run_event(
                run["id"], "session.recovery_start_failed",
                {
                    "assignment_id": assignment["id"],
                    "project_id": project_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return
        self.store.append_run_event(
            run["id"], "session.recovered",
            {
                "assignment_id": assignment["id"],
                "session_id": assignment["session_id"],
                "member": assignment["member"],
                "epoch": assignment["epoch"],
                "project_id": project_id,
            },
        )

    def _idle_member(self, run: dict) -> str | None:
        """Pick a Member holding no seat in any live run.

        Scoping this to one run was wrong: a Member seated on another live run
        is not idle, and handing it a second seat means the receiving seat gets
        a Member that cannot write (one seat per Member is enforced by a unique
        index), so the handoff silently achieved nothing.
        """
        from backend.core.config import MEMBER_NAMES

        taken = {
            assignment["member"]
            for assignment in self.store.assignments(run["id"], active_only=True)
        }
        try:
            with self.state.db.connect() as connection:
                rows = connection.execute(
                    """SELECT DISTINCT a.member
                         FROM competition_assignments a
                         JOIN competition_runs r ON r.id=a.run_id
                        WHERE a.released_at IS NULL
                          AND r.status NOT IN ('finished','stopped')""",
                ).fetchall()
            taken.update(row["member"] for row in rows)
        except Exception:
            _recovery_logger.debug('Error handled in _idle_member', exc_info=True)
        for name in MEMBER_NAMES:
            if name not in taken:
                return name
        return None

    def _expire_at_competition_end(self, run: dict) -> None:
        value = run["config_snapshot"]["workflow_spec"].get("ends_at")
        ends_at = _timestamp(value) if value else None
        if ends_at is None or ends_at > datetime.now(timezone.utc):
            return
        with self.state.db.connect() as connection:
            connection.execute(
                """UPDATE competition_challenges SET state='expired'
                   WHERE id IN (SELECT challenge_id FROM competition_run_challenges WHERE run_id=%s)
                   AND state NOT IN ('solved','expired','withdrawn','cancelled')""",
                (run["id"],),
            )

    deadline_grace_seconds: int = 120

    def _run_ends_at(self, run: dict) -> datetime | None:
        """Declared end of the task: platform/competition first, local guard last.

        ends_at is an absolute timestamp the competition or platform declares;
        when present it is authoritative and is never shortened here.
        duration_minutes is only a local guard: 0 disables it, and the config
        fallback run_deadline_guard_minutes is itself 0 by default, so a run
        normally ends when the queue drains or the platform closes the task.
        """

        spec = (run.get("config_snapshot") or {}).get("workflow_spec") or {}
        ends = _timestamp(spec.get("ends_at")) if spec.get("ends_at") else None
        if ends is not None:
            return ends
        try:
            minutes = float(spec.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes <= 0:
            runtime = getattr(getattr(getattr(self, "state", None), "config", None), "runtime", None)
            try:
                minutes = float(getattr(runtime, "run_deadline_guard_minutes", 0) or 0)
            except (TypeError, ValueError):
                minutes = 0.0
        created = _timestamp(run.get("created_at"))
        if minutes > 0 and created is not None:
            return created + timedelta(minutes=minutes)
        return None

    def _validate_run_window(self, run: dict, platform=None) -> None:
        """Never let a local guard end the run before the declared window.

        A benchmark run in the 2026-10-25 replay carried a stale 180-minute
        local limit while the platform window was six hours, so the run stopped
        three hours early with fourteen challenges never started. When both a
        duration and a platform/competition window are known, the shorter local
        guard is promoted to the declared window and the mismatch is recorded.
        """

        spec = (run.get("config_snapshot") or {}).get("workflow_spec") or {}
        created = _timestamp(run.get("created_at"))
        if created is None:
            return
        try:
            local = float(spec.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            local = 0.0
        if local <= 0:
            return
        declared = _timestamp(spec.get("ends_at")) if spec.get("ends_at") else None
        window_minutes: float | None = None
        source = ""
        if declared is not None:
            window_minutes = max(0.0, (declared - created).total_seconds() / 60.0)
            source = "competition"
        if window_minutes is None and platform is not None:
            method = getattr(platform, "window_minutes", None)
            if callable(method):
                with suppress(Exception):
                    value = method()
                    if value is not None:
                        window_minutes = float(value)
                        source = "platform"
        if window_minutes is None or local >= window_minutes:
            return
        adjusted = int(window_minutes) + (1 if float(window_minutes) % 1 else 0)
        local_end = created + timedelta(minutes=local)
        with suppress(Exception), self.state.db.connect() as connection:
            connection.execute(
                """UPDATE competition_runs
                   SET config_snapshot = jsonb_set(
                         config_snapshot, '{workflow_spec,duration_minutes}',
                         %s::jsonb, true)
                   WHERE id=%s""",
                (json.dumps(adjusted), run["id"]),
            )
        snapshot = run.get("config_snapshot")
        if isinstance(snapshot, dict):
            snapshot.setdefault("workflow_spec", {})["duration_minutes"] = adjusted
        self.store.append_run_event(
            run["id"], "run.window_mismatch",
            {
                "source": source or "declared",
                "local_guard_minutes": local,
                "window_minutes": round(window_minutes, 2),
                "promoted_to_minutes": adjusted,
                "local_ends_at": local_end.isoformat(),
                "declared_ends_at": declared.isoformat() if declared else None,
            },
        )

    def _enforce_deadline(self, run: dict) -> None:
        """Drain and finish the run when its task window is over.

        This is the local half of the platform deadline: the platform stops
        scoring at its own clock, so the last two minutes are spent stopping
        projects and closing containers rather than starting new work.
        """

        ends_at = self._run_ends_at(run)
        if ends_at is None:
            return
        now = datetime.now(timezone.utc)
        if now < ends_at - timedelta(seconds=self.deadline_grace_seconds):
            return
        if run["status"] in {"running", "paused"}:
            target = "draining" if run["status"] == "running" else "stopped"
            try:
                run = self.store.transition_run(
                    run["id"], target, revision=run["revision"], permit_active=True,
                )
            except CompetitionConflict:
                return
            self.store.append_run_event(
                run["id"], "run.deadline_reached",
                {"ends_at": ends_at.isoformat(), "status": target},
            )
            if target == "stopped":
                self._teardown_stopped_run(run["id"], run)
                self.store.append_run_event(run["id"], "run.stopped", {"reason": "deadline"})
                return
        if run["status"] != "draining":
            return
        with self.state.db.connect() as connection:
            connection.execute(
                """UPDATE competition_challenges SET state='expired'
                   WHERE id IN (SELECT challenge_id FROM competition_run_challenges WHERE run_id=%s)
                   AND state NOT IN ('solved','expired','withdrawn','cancelled')""",
                (run["id"],),
            )
        self._teardown_stopped_run(run["id"], run)
        current = self.store.get_run(run["id"])
        if current["status"] == "draining":
            self.store.transition_run(
                run["id"], "finished", revision=current["revision"], permit_active=True,
            )
            self.store.append_run_event(run["id"], "run.finished", {"reason": "deadline"})
            self._write_run_summary(self.store.get_run(run["id"]))

    def _record_observation(self, run: dict) -> None:
        """Sample durable pressure metrics without making them tick-critical."""
        now = time.monotonic()
        if now - self._last_observation_at < self.observation_interval:
            return
        try:
            self.store.observe_run(
                run["id"], self.owner, resource_capacity=self._resource_capacity.get(run["id"]),
            )
            self._last_observation_at = now
        except Exception as exc:
            _recovery_logger.debug('Error handled in _record_observation', exc_info=True)
            self.store.append_run_event(
                run["id"], "observation.failed", {"error": str(exc)}
            )

    def _stop_expired_challenges(self, run: dict) -> None:
        orchestrator = self.state.orchestrator
        platform = self._platform_for_run(run)
        for challenge in self.store.challenges(run["id"]):
            if challenge["state"] != "expired":
                continue
            with self.state.db.connect() as connection:
                claimed = connection.execute(
                    """UPDATE competition_challenges SET metadata=metadata || %s
                       WHERE id=%s AND state='expired'
                       AND COALESCE(metadata->>'expiry_cleanup_state','')<>'done'
                       AND (metadata->>'expiry_cleanup_after' IS NULL
                            OR (metadata->>'expiry_cleanup_after')::timestamptz<=now())
                       RETURNING id""",
                    (Jsonb({"expiry_cleanup_state": "running", "expiry_cleanup_after": (
                        datetime.now(timezone.utc) + timedelta(seconds=60)
                    ).isoformat()}), challenge["id"]),
                ).fetchone()
            if claimed is None:
                continue
            try:
                if challenge.get("project_id"):
                    if orchestrator is not None:
                        orchestrator.stop_project(challenge["project_id"])
                    self._release_project_engine(challenge["project_id"])
                if self._instance_ready(challenge["id"]):
                    self._stop_instance(run, challenge, platform=platform)
            except Exception as exc:
                _recovery_logger.debug("Challenge cleanup failed", exc_info=True)
                self.store.append_run_event(
                    run["id"], "challenge.cleanup_failed",
                    {"challenge_id": challenge["id"], "error": str(exc), "retry_after": 60},
                )
                continue
            with self.state.db.connect() as connection:
                connection.execute(
                    "UPDATE competition_challenges SET metadata=metadata || %s WHERE id=%s",
                    (Jsonb({"expiry_cleanup_state": "done"}), challenge["id"]),
                )

    def _release_external_solved(self, run: dict, challenge: dict) -> None:
        """Release local resources for a challenge solved outside this run."""
        orchestrator = self.state.orchestrator
        if challenge.get("project_id"):
            if orchestrator is not None:
                with suppress(Exception):
                    orchestrator.stop_project(challenge["project_id"])
            self._release_project_engine(challenge["project_id"])
        for assignment in self.store.assignments(run["id"], active_only=True):
            if assignment["challenge_id"] == challenge["id"]:
                self.store.release(
                    assignment["id"], assignment["lease_owner"], assignment["epoch"]
                )
        if self._instance_ready(challenge["id"]):
            self._stop_instance(run, challenge)

    def _advance_run_completion(self, run: dict) -> None:
        terminal = {"solved", "expired", "withdrawn", "cancelled"}
        challenges = self.store.challenges(run["id"])
        current = self.store.get_run(run["id"])
        ends_at = _timestamp(run["config_snapshot"]["workflow_spec"].get("ends_at"))
        before_declared_end = ends_at is not None and ends_at > datetime.now(timezone.utc)
        if current["status"] == "running" and challenges and not before_declared_end and all(
            item["state"] in terminal for item in challenges
        ):
            self._stop_run_projects(run["id"])
            self.store.release_run_assignments(run["id"])
            current = self.store.transition_run(
                run["id"], "draining", revision=current["revision"]
            )
            self.store.append_run_event(run["id"], "run.draining", {})
        if current["status"] != "draining":
            return
        with self.state.db.connect() as connection:
            outstanding = connection.execute(
                """SELECT 1 FROM competition_wp_jobs
                   WHERE run_id=%s AND status NOT IN ('done','failed') LIMIT 1""",
                (run["id"],),
            ).fetchone()
            occupied = connection.execute(
                """SELECT 1 FROM competition_assignments
                   WHERE run_id=%s AND released_at IS NULL LIMIT 1""",
                (run["id"],),
            ).fetchone()
        if not outstanding and not occupied:
            current = self.store.get_run(run["id"])
            self.store.transition_run(
                run["id"], "finished", revision=current["revision"]
            )
            self.store.append_run_event(run["id"], "run.finished", {})
            self._write_run_summary(self.store.get_run(run["id"]))

    def _sync_orchestrator_assignments(self, run: dict) -> None:
        orchestrator = self.state.orchestrator
        if orchestrator is None:
            return
        owners = orchestrator.active_member_owners()
        challenges = {item.get("project_id"): item for item in self.store.challenges(run["id"])}
        active = self.store.assignments(run["id"], active_only=True)
        active_start_keys = {
            (challenge.get("project_id"), assignment["member"])
            for assignment in active
            for challenge in self.store.challenges(run["id"])
            if challenge["id"] == assignment["challenge_id"] and challenge.get("project_id")
        }
        self._starting_members.intersection_update(active_start_keys)
        self._starting_projects.intersection_update({key[0] for key in active_start_keys})
        for assignment in active:
            if assignment["lease_owner"] != self.owner:
                continue
            project_id = next(
                (
                    project for project, challenge in challenges.items()
                    if challenge["id"] == assignment["challenge_id"]
                ),
                None,
            )
            owner_project = owners.get(assignment["member"])
            start_key = (project_id, assignment["member"])
            if owner_project == project_id:
                self._starting_projects.discard(project_id)
                self._starting_members.discard(start_key)
            elif owner_project is not None and owner_project != project_id:
                self._starting_members.discard(start_key)
                self.store.release(
                    assignment["id"], assignment["lease_owner"], assignment["epoch"]
                )
            elif owner_project is None and project_id and assignment["role"] != "wp":
                if start_key in self._starting_members and not orchestrator.startup_in_progress(project_id):
                    self._starting_members.discard(start_key)
                    if not any(key[0] == project_id for key in self._starting_members):
                        self._starting_projects.discard(project_id)
                if start_key not in self._starting_members:
                    if not self._claim_project_engine(run, project_id):
                        continue
                    self._starting_members.add(start_key)
                    if orchestrator.startup_in_progress(project_id):
                        continue
                    self._starting_projects.add(project_id)
                    try:
                        orchestrator.start_project_async(project_id)
                    except Exception as exc:
                        _recovery_logger.debug('Error handled in _sync_orchestrator_assignments', exc_info=True)
                        self._starting_members = {
                            key for key in self._starting_members
                            if key[0] != project_id
                        }
                        self._starting_projects.discard(project_id)
                        self.store.append_run_event(
                            run["id"], "assignment.recovery_failed",
                            {"challenge_id": assignment["challenge_id"], "member": assignment["member"], "error": str(exc)},
                        )
        for member, project_id in owners.items():
            challenge = challenges.get(project_id)
            if challenge is None or self.store.find_assignment(
                run["id"], challenge["id"], member
            ):
                continue
            occupied = [
                item for item in self.store.assignments(run["id"], active_only=True)
                if item["challenge_id"] == challenge["id"]
            ]
            role = "primary" if not occupied else "helper"
            try:
                assignment = self.store.assign(
                    run["id"], challenge["id"], member, self.owner, role=role
                )
            except CompetitionConflict:
                continue
            self._starting_projects.discard(project_id)
            self.store.append_run_event(
                run["id"], "assignment.started",
                {"challenge_id": challenge["id"], "member": member, "role": role,
                 "session_id": assignment["session_id"]},
            )

    def _park_stalled_challenges(self, run: dict, challenges, now) -> list[str]:
        """Release the slot of a challenge that has held it past its deadline.

        Parking is not a failure: the project is stopped, the container closed,
        the evidence stays on disk, and the challenge re-enters the queue after
        a short backoff. The handoff note is written before the challenge is
        marked ready, so the next attempt reads it in its first turn instead of
        after a concurrency-dependent background write.
        """

        parked = park_candidates(
            challenges, now=now, per_challenge_minutes=self.per_challenge_minutes,
        )
        if not parked:
            return []
        by_id = {challenge.id: challenge for challenge in challenges}
        orchestrator = self.state.orchestrator
        platform = self._platform_for_run(run)
        released: list[str] = []
        next_rank = self._next_parked_rank(run["id"])
        for challenge_id in parked:
            challenge = by_id.get(challenge_id)
            if challenge is None:
                continue
            row = next(
                (item for item in self.store.challenges(run["id"]) if item["id"] == challenge_id),
                None,
            )
            if row is None:
                continue
            attempts = int(challenge.attempts or 0) + 1
            backoff = park_backoff_minutes(attempts)
            with suppress(Exception):
                if row.get("project_id") and orchestrator is not None:
                    orchestrator.stop_project(row["project_id"])
                if row.get("project_id"):
                    self._release_project_engine(row["project_id"])
                self.store.release_challenge_assignments(run["id"], challenge_id)
            with suppress(Exception):
                if self._instance_ready(challenge_id):
                    self._submit_stop(run, row, platform=platform)
            digest_ready = self._queue_park_digest(run, row, attempts, backoff)
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_challenges
                       SET state='ready', metadata=metadata || %s
                       WHERE id=%s AND state IN ('solving','ready')""",
                    (Jsonb({
                        "attempts": attempts,
                        "retry_at": (now + timedelta(minutes=backoff)).isoformat(),
                        "parked_at": now.isoformat(),
                        "parked_rank": next_rank,
                        "park_reason": f"soft-deadline-{int(self.per_challenge_minutes)}m",
                        "park_digest_ready": bool(digest_ready),
                    }), challenge_id),
                )
            self.store.append_run_event(
                run["id"], "challenge.parked",
                {"challenge_id": challenge_id, "attempts": attempts,
                 "retry_in_minutes": backoff, "parked_rank": next_rank,
                 "digest_ready": bool(digest_ready),
                 "reason": "soft-deadline"},
            )
            next_rank += 1
            self._maybe_buy_hint(run, row, attempts, platform)
            released.append(challenge_id)
        return released

    def _next_parked_rank(self, run_id: str) -> int:
        """Monotonic per-run rank: fresh work keeps 0, parked work queues FIFO."""

        try:
            with self.state.db.connect() as connection:
                row = connection.execute(
                    """SELECT COALESCE(MAX((metadata->>'parked_rank')::int),0) AS max_rank
                       FROM competition_challenges
                       WHERE (metadata->>'parked_rank') ~ '^[0-9]+$'
                         AND id IN (SELECT challenge_id FROM competition_run_challenges
                                    WHERE run_id=%s)""",
                    (run_id,),
                ).fetchone()
        except Exception:
            _recovery_logger.debug("Error handled in _next_parked_rank", exc_info=True)
            return 1
        return int((row or {}).get("max_rank") or 0) + 1

    def _maybe_buy_hint(self, run: dict, row: dict, attempts: int, platform) -> None:
        """Buy the platform hint once the challenge has been parked enough times.

        A hint discounts that challenge's remaining flags, so it is only worth
        buying for a target the run has already failed on more than once and
        still has window left to finish.
        """

        if not self.hint_at_attempt or attempts < self.hint_at_attempt:
            return
        metadata = row.get("metadata") or {}
        if metadata.get("hint_bought"):
            return
        remaining_minutes = self._remaining_minutes(run)
        if remaining_minutes is not None and remaining_minutes < self.hint_min_remaining_minutes:
            return
        external_id = str(row.get("external_id") or "")
        try:
            result = platform.hint(external_id)
        except Exception as exc:
            _recovery_logger.debug("Error handled in _maybe_buy_hint", exc_info=True)
            self.store.append_run_event(
                run["id"], "challenge.hint_failed",
                {"challenge_id": row["id"], "attempts": attempts, "error": str(exc)[:300]},
            )
            return
        hint = ""
        if isinstance(result, dict):
            hint = str(result.get("hint") or "").strip()
            if not hint and result.get("reason"):
                self.store.append_run_event(
                    run["id"], "challenge.hint_unavailable",
                    {"challenge_id": row["id"], "attempts": attempts, "reason": str(result["reason"])[:120]},
                )
                return
        if not hint:
            self.store.append_run_event(
                run["id"], "challenge.hint_empty",
                {"challenge_id": row["id"], "attempts": attempts},
            )
            return
        description = "【平台提示 · 扣分题】" + hint[:1600]
        project_id = row.get("project_id")
        with self.state.db.connect() as connection:
            if project_id:
                reserved = node_store.reserve_fact(connection, project_id, description)
                node_store.insert_fact(connection, project_id, reserved.id, description)
            connection.execute(
                """UPDATE competition_challenges
                   SET metadata = metadata || %s WHERE id=%s""",
                (Jsonb({"hint_bought": True, "hint_attempt": attempts, "hint_text": hint[:1600]}),
                 row["id"]),
            )
        self.store.append_run_event(
            run["id"], "challenge.hint_bought",
            {"challenge_id": row["id"], "attempts": attempts,
             "chars": len(hint), "remaining_minutes": remaining_minutes},
        )

    def _remaining_minutes(self, run: dict) -> float | None:
        ends_at = self._run_ends_at(run)
        if ends_at is None:
            return None
        return (ends_at - datetime.now(timezone.utc)).total_seconds() / 60.0

    @staticmethod
    def _p95(values: list[int]) -> int | None:
        if not values:
            return None
        ordered = sorted(int(value) for value in values)
        index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
        return ordered[index]

    def _run_summary(self, run: dict) -> dict:
        """Operator-facing scorecard: window, slots, challenges and model.

        The four groups come from the plan's acceptance criteria so a run can
        be audited without reading the event stream.
        """

        spec = (run.get("config_snapshot") or {}).get("workflow_spec") or {}
        created = _timestamp(run.get("created_at"))
        now = datetime.now(timezone.utc)
        ends_at = self._run_ends_at(run)
        try:
            local_guard = float(spec.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            local_guard = 0.0
        if spec.get("ends_at"):
            window_source = "competition"
        elif local_guard > 0:
            window_source = "local_guard"
        else:
            window_source = "unbounded"
        window = {
            "source": window_source,
            "minutes": (
                round((ends_at - created).total_seconds() / 60.0, 2)
                if ends_at is not None and created is not None else None
            ),
            "local_guard_minutes": local_guard,
        }
        rows = self.store.challenges(run["id"])
        total = len(rows)
        parked = 0
        solved = 0
        running = 0
        for row in rows:
            metadata = row.get("metadata") or {}
            try:
                attempts = int(metadata.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            parked += 1 if attempts > 0 else 0
            state = row.get("state")
            solved += 1 if state == "solved" else 0
            running += 1 if state == "solving" else 0
        attempted = 0
        resumed = 0
        turnover: list[int] = []
        assigned_seconds = 0.0
        with suppress(Exception), self.state.db.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT challenge_id) AS attempted
                   FROM competition_assignments
                   WHERE run_id=%s AND role='primary'""",
                (run["id"],),
            ).fetchone()
            attempted = int((row or {}).get("attempted") or 0)
        with suppress(Exception), self.state.db.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS resumed FROM competition_run_events
                   WHERE run_id=%s AND kind='session.resumed'""",
                (run["id"],),
            ).fetchone()
            resumed = int((row or {}).get("resumed") or 0)
        with suppress(Exception), self.state.db.connect() as connection:
            events = connection.execute(
                """SELECT (payload->>'ms')::int AS ms FROM competition_run_events
                   WHERE run_id=%s AND kind='slot.turnover_ms'
                   ORDER BY event_id DESC LIMIT 200""",
                (run["id"],),
            ).fetchall()
            turnover = [
                int(item["ms"]) for item in events if item.get("ms") is not None
            ]
        with suppress(Exception), self.state.db.connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (
                       COALESCE(a.released_at, now()) - a.created_at))),0) AS seconds
                   FROM competition_assignments a
                   JOIN competition_challenges c ON c.id=a.challenge_id
                   WHERE a.run_id=%s AND a.role='primary'
                     AND c.metadata->>'remote'='true'""",
                (run["id"],),
            ).fetchone()
            assigned_seconds = float((row or {}).get("seconds") or 0)
        slot_limit = 0
        with suppress(Exception):
            slot_limit = int(self._remote_instance_limit(run, self._platform_for_run(run)) or 0)
        elapsed = max(1.0, (now - created).total_seconds()) if created else 1.0
        utilization = (
            min(1.0, assigned_seconds / (slot_limit * elapsed)) if slot_limit else 0.0
        )
        config = getattr(self.state, "config", None)
        member = getattr(config, "member", None)
        backup = getattr(config, "member_backup", None)
        key_slot = "primary"
        if backup is not None and getattr(backup, "configured", False):
            key_slot = "primary+backup (5/5)"
        return {
            "run_id": run.get("id"),
            "status": run.get("status"),
            "generated_at": now.isoformat(),
            "window": window,
            "slots": {
                "limit": slot_limit,
                "utilization": round(utilization, 4),
                "turnover_p95_ms": self._p95(turnover),
                "turnover_samples": len(turnover),
            },
            "challenges": {
                "total": total,
                "attempted": attempted,
                "solved": solved,
                "running": running,
                "parked": parked,
                "resumed": resumed,
            },
            "model": {
                "id": getattr(member, "model", ""),
                "surface": getattr(member, "api_surface", "auto"),
                "key_slot": key_slot,
            },
        }

    def _write_run_summary(self, run: dict) -> None:
        """Persist summary.json next to the exports so the run is auditable."""

        try:
            summary = self._run_summary(run)
            root = Path(
                getattr(self.state, "artifact_root", "data/artifacts")
            ) / "exports" / "competition"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{run.get('id')}-summary.json"
            path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            self.store.append_run_event(
                run["id"], "run.summary", {"path": str(path)},
            )
        except Exception:
            _recovery_logger.debug("Error handled in _write_run_summary", exc_info=True)

    def _queue_park_digest(self, run: dict, row: dict, attempts: int, backoff: int) -> bool:
        """Consolidate the parked attempt before the challenge is ready again.

        The wait is bounded (park_digest_timeout, 90 seconds by default). A slow
        or failing model is replaced by the deterministic template, so parking
        releases the slot on time while still leaving a handoff in facts.
        """

        if not self.park_digests_enabled or not row.get("project_id"):
            return False
        challenge_id = row["id"]
        if challenge_id in self._digest_futures:
            return False
        deadline = time.monotonic() + self.park_digest_timeout
        try:
            future = self._digest_pool.submit(
                self._run_park_digest, run, dict(row), attempts, backoff, deadline,
            )
        except RuntimeError:
            return self._write_park_fallback(
                run, dict(row), attempts, backoff, reason="pool_closed",
            )
        self._digest_futures[challenge_id] = future
        try:
            return bool(future.result(timeout=self.park_digest_timeout))
        except FuturesTimeoutError:
            self.store.append_run_event(
                run["id"], "challenge.summary_timeout",
                {"challenge_id": challenge_id, "attempts": attempts,
                 "timeout_seconds": self.park_digest_timeout},
            )
            return self._write_park_fallback(
                run, dict(row), attempts, backoff, reason="timeout",
            )
        except Exception as exc:
            _recovery_logger.debug("Error handled in _queue_park_digest", exc_info=True)
            with suppress(Exception):
                self.store.append_run_event(
                    run["id"], "challenge.summary_failed",
                    {"challenge_id": challenge_id, "attempts": attempts,
                     "reason": "scheduler_exception", "error": str(exc)[:300]},
                )
            return self._write_park_fallback(
                run, dict(row), attempts, backoff, reason="exception",
            )
        finally:
            self._digest_futures.pop(challenge_id, None)

    def _write_park_fallback(
        self, run: dict, row: dict, attempts: int, backoff: int, *, reason: str = ""
    ) -> bool:
        """Write the deterministic handoff, used when the model path does not."""

        events: list[dict] = []
        try:
            session_id = self._latest_session_id(run["id"], row["id"])
            if session_id:
                events = self.store.events(session_id, limit=120)
        except Exception:
            _recovery_logger.debug("Error handled in _write_park_fallback events", exc_info=True)
        digest = _fallback_park_digest(row, events, attempts, backoff)
        try:
            self._store_park_digest(
                run, row, attempts, digest, source="fallback", events=len(events),
            )
        except Exception as exc:
            _recovery_logger.debug("Error handled in _write_park_fallback write", exc_info=True)
            with suppress(Exception):
                self.store.append_run_event(
                    run["id"], "challenge.summary_failed",
                    {"challenge_id": row.get("id"), "attempts": attempts,
                     "reason": reason or "write_failed", "error": str(exc)[:300]},
                )
            return False
        with suppress(Exception):
            self.store.append_run_event(
                run["id"], "challenge.park_digest_fallback",
                {"challenge_id": row.get("id"), "attempts": attempts, "reason": reason},
            )
        return True

    def _store_park_digest(
        self, run: dict, row: dict, attempts: int, digest: str, *,
        source: str, events: int,
    ) -> None:
        challenge_id = row["id"]
        project_id = row.get("project_id")
        description = f"【park 交接 · 第 {attempts} 次】" + digest.strip()[:2900]
        with self.state.db.connect() as connection:
            reserved = node_store.reserve_fact(connection, project_id, description)
            node_store.insert_fact(connection, project_id, reserved.id, description)
            connection.execute(
                """UPDATE competition_challenges
                   SET metadata = metadata || %s WHERE id=%s""",
                (Jsonb({
                    "park_digest": digest.strip()[:2400],
                    "park_digest_attempt": attempts,
                }), challenge_id),
            )
        self.store.append_run_event(
            run["id"], "challenge.park_digest",
            {"challenge_id": challenge_id, "attempts": attempts,
             "chars": len(description), "events": events,
             "source": source},
        )

    def _run_park_digest(
        self, run: dict, row: dict, attempts: int, backoff: int,
        deadline: float | None = None,
    ) -> bool:
        challenge_id = row["id"]
        project_id = row.get("project_id")
        try:
            session_id = self._latest_session_id(run["id"], challenge_id)
            if session_id:
                with suppress(Exception):
                    AgentJobStore(self.state.db).mark_lost_for_session(session_id)
            events = self.store.events(session_id, limit=400) if session_id else []
            job_lines = self._park_job_lines(project_id)
            digest = ""
            source = "fallback"
            if events:
                digest = self._ask_for_park_digest(row, events, attempts, backoff)
                if digest:
                    source = "model"
            if not digest:
                digest = _fallback_park_digest(row, events, attempts, backoff)
            if job_lines:
                digest = digest.rstrip() + chr(10) + chr(10) + job_lines
            if deadline is not None and time.monotonic() > deadline:
                with suppress(Exception):
                    self.store.append_run_event(
                        run["id"], "challenge.summary_late",
                        {"challenge_id": challenge_id, "attempts": attempts,
                         "source": source},
                    )
                return False
            self._store_park_digest(
                run, row, attempts, digest, source=source, events=len(events),
            )
            return True
        except Exception as exc:
            _recovery_logger.debug("Error handled in _run_park_digest", exc_info=True)
            with suppress(Exception):
                self.store.append_run_event(
                    run["id"], "challenge.summary_failed",
                    {"challenge_id": challenge_id, "attempts": attempts,
                     "error": str(exc)[:300]},
                )
            return False

    def _park_job_lines(self, project_id: str) -> str:
        """One line per finished background job, straight from the job table.

        The Sugilite curator is the designed summarizer, but it is off for
        pentest projects and its digest only surfaces on the next context build.
        Parking is the one moment where the attempt's jobs are certainly over,
        so the handoff carries their outcome itself.
        """

        try:
            with self.state.db.connect() as connection:
                rows = connection.execute(
                    """SELECT command,state,exit_code,digest_json FROM agent_jobs
                       WHERE project_id=%s AND state IN ('exited','lost','cancelled')
                       ORDER BY finished_at DESC NULLS LAST LIMIT 6""",
                    (project_id,),
                ).fetchall()
        except Exception:  # noqa: BLE001 - a missing job table must not fail a park
            return ""
        if not rows:
            return ""
        lines = ["【后台作业结果（容器已销毁）】"]
        for row in rows:
            command = " ".join(str(row.get("command") or "").split())[:110]
            state = str(row.get("state") or "?")
            code = row.get("exit_code")
            digest = row.get("digest_json") or {}
            tail = ""
            if isinstance(digest, dict):
                tail = str(digest.get("summary") or "").strip().replace(chr(10), " ")[:100]
            suffix = "" if code is None else " " + str(code)
            lines.append(
                "- [" + state + suffix + "] " + command + ((" => " + tail) if tail else "")
            )
        return chr(10).join(lines)

    def _latest_session_id(self, run_id: str, challenge_id: str) -> str:
        with self.state.db.connect() as connection:
            row = connection.execute(
                """SELECT id FROM competition_sessions
                   WHERE run_id=%s AND challenge_id=%s
                   ORDER BY created_at DESC LIMIT 1""",
                (run_id, challenge_id),
            ).fetchone()
        return str(row["id"]) if row else ""

    def _ask_for_park_digest(self, row: dict, events: list[dict], attempts: int, backoff: int) -> str:
        config = getattr(self.state.config, "member", None)
        if config is None or not config.configured or config.api_format == "mock":
            return ""
        nl = chr(10)
        transcript = json.dumps(events, ensure_ascii=False)[:40_000]
        prompt = (
            "This attempt on one challenge hit its soft deadline, so its container was closed. "
            + "It will be retried in about " + str(int(backoff)) + " minutes as attempt "
            + str(int(attempts)) + ", possibly by a different Member that sees only your note. "
            + "Write a compact handoff in Simplified Chinese, at most 260 words, with exactly five sections: "
            + "1) 已确认事实 (exact paths/commands/hashes), 2) 已排除方向 (hypothesis + why it failed), "
            + "3) 最后 3 条命令与输出要点, 4) 容器地址与端口 (services, ports, credentials, files), "
            + "5) 下一步建议 (2-3 concrete commands). "
            + "Never invent a flag or a fact outside the transcript. Never write 'keep exploring'; "
            + "every next step must be a command a fresh container can run."
            + nl + "Challenge: " + str(row.get("external_id")) + " attempt " + str(int(attempts))
            + nl + "Persisted session events:" + nl + transcript
        )
        adapter = make_adapter(config, name="park-digest")
        return str(adapter.chat(
            [{"role": "user", "content": prompt}],
            system_prompt=PARK_DIGEST_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=1024,
        ) or "").strip()

    def _policy_challenges(self, rows: list[dict], active: list[dict]) -> list[Challenge]:
        """Project durable rows into the pure scheduler's immutable view."""

        members_by_challenge: dict[str, list[str]] = {}
        for assignment in active:
            if assignment["role"] != "wp":
                members_by_challenge.setdefault(assignment["challenge_id"], []).append(
                    assignment["member"]
                )
        challenges: list[Challenge] = []
        for row in rows:
            metadata = row["metadata"] or {}
            ready_at = _timestamp(metadata.get("ready_at")) or datetime.now(timezone.utc)
            raw_solve_count = metadata.get("solve_count")
            try:
                solve_count = int(raw_solve_count) if raw_solve_count is not None else None
            except (TypeError, ValueError):
                solve_count = None
            try:
                score = float(metadata.get("score")) if metadata.get("score") is not None else None
            except (TypeError, ValueError):
                score = None
            try:
                attempts = int(metadata.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            try:
                parked_rank = int(metadata.get("parked_rank") or 0)
            except (TypeError, ValueError):
                parked_rank = 0
            challenges.append(Challenge(
                row["id"], metadata.get("category", "misc"), ready_at,
                remote=bool(metadata.get("remote")),
                instance_ready=self._instance_ready(row["id"]),
                first_assigned_at=_timestamp(row.get("first_assigned_at")),
                attempt_started_at=_timestamp(row.get("attempt_started_at")),
                deadline_at=_timestamp(row.get("deadline_at")),
                members=tuple(members_by_challenge.get(row["id"], [])),
                state=row["state"],
                solve_count=solve_count,
                difficulty=(str(metadata.get("difficulty") or "").strip().lower() or None),
                score=score,
                attempts=attempts,
                retry_at=_timestamp(metadata.get("retry_at")),
                parked_rank=parked_rank,
                parked_at=_timestamp(metadata.get("parked_at")),
            ))
        return challenges

    def _schedule_projects(self, run: dict) -> None:
        orchestrator = self.state.orchestrator
        if orchestrator is None:
            return
        platform = self._platform_for_run(run)
        rows = self.store.challenges(run["id"])
        active = self.store.assignments(run["id"], active_only=True)
        if not active:
            self._restore_paused_seats(run, rows)
            active = self.store.assignments(run["id"], active_only=True)
        challenges = self._policy_challenges(rows, active)
        remote_limit = self._remote_instance_limit(run, platform)
        local_pending = 0
        with self.state.db.connect() as connection:
            local_ids = {
                str(row["external_id"])
                for row in connection.execute(
                    """SELECT external_id FROM competition_instances
                       WHERE run_id=%s AND state NOT IN ('stopped','expired')""",
                    (run["id"],),
                ).fetchall()
            }
            # A prestarted/lookahead instance holds a platform slot before its
            # challenge is assigned. Counting only member-occupied challenges
            # let the planner start a fourth container and the platform
            # answered 409 "max active", which used to end the whole run.
            pending = connection.execute(
                """SELECT COUNT(*) AS pending FROM competition_instances i
                   WHERE i.run_id=%s
                     AND i.state IN ('ready','running','active','start_pending','stop_pending')
                     AND NOT EXISTS (
                       SELECT 1 FROM competition_assignments a
                       WHERE a.challenge_id=i.challenge_id AND a.released_at IS NULL)""",
                (run["id"],),
            ).fetchone()
            local_pending = int((pending or {}).get("pending") or 0)
        external_used = 0
        try:
            for item in platform.instances():
                if isinstance(item, dict):
                    external_id = str(
                        item.get("external_id", item.get("externalId", item.get("id", "")))
                    )
                    state = str(item.get("state", item.get("status", "ready"))).lower()
                    instance_active = state not in {"stopped", "expired", "deleted", "terminated"}
                else:
                    external_id = str(item)
                    instance_active = True
                if instance_active and external_id not in local_ids:
                    external_used += 1
        except Exception as exc:
            _recovery_logger.debug('Error handled in _schedule_projects', exc_info=True)
            external_used = remote_limit
            self.store.append_run_event(
                run["id"], "instance.quota_unknown", {"error": str(exc)}
            )
        scheduler_now = datetime.now(timezone.utc)
        ends_at = self._run_ends_at(run)
        started_at = _timestamp(run.get("created_at"))
        if self._park_stalled_challenges(run, challenges, scheduler_now):
            self._prestart_lookahead(run)
            return
        plans = plan_assignments(
            challenges,
            occupied_members={item["member"] for item in active},
            remote_capacity=max(0, remote_limit - external_used - local_pending),
            now=scheduler_now,
            local_capacity=self._local_task_limit(run),
            available_members={member.name for member in self.state.config.available_members()},
            phase=phase_for(scheduler_now, started_at, ends_at),
            remaining_seconds=(
                (ends_at - scheduler_now).total_seconds() if ends_at else None
            ),
        )
        planned_ids = {plan.challenge_id for plan in plans}
        local_limit = self._local_task_limit(run)
        local_used = sum(
            bool(item.members) and not item.remote for item in challenges
        )
        remote_used = sum(
            bool(item.members) and item.remote for item in challenges
        )
        _, remote_source = self._resolve_remote_limit(
            run["config_snapshot"]["workflow_spec"], platform, run.get("limits"),
        )
        self._resource_capacity[run["id"]] = {
            "local": {"limit": local_limit, "source": "run" if "max_concurrent_tasks" in (run.get("limits") or {}) else "config"},
            "remote": {"limit": remote_limit, "source": remote_source, "external_used": external_used},
        }
        for item in challenges:
            if item.id in planned_ids or item.members or item.state not in {"ready", "solving"}:
                continue
            if item.remote and remote_used >= remote_limit:
                reason = "waiting for remote container capacity"
            elif not item.remote and local_used >= local_limit:
                reason = "waiting for local task capacity"
            elif not self.state.config.available_members():
                reason = "waiting for configured Member seat"
            else:
                reason = "waiting for scheduler tick"
            with self.state.db.connect() as connection:
                connection.execute(
                    "UPDATE competition_challenges SET metadata=metadata || %s WHERE id=%s",
                    (Jsonb({"queue_reason": reason}), item.id),
                )
        by_id = {item["id"]: item for item in rows}
        for plan in plans:
            challenge = by_id[plan.challenge_id]
            project_id = challenge.get("project_id")
            if not project_id:
                continue
            if not self._claim_project_engine(run, project_id):
                continue
            start_key = (project_id, plan.member)
            if start_key in self._starting_members:
                continue
            if plan.role == "primary" and project_id in self._starting_projects:
                continue
            prestarted = self._prestart_futures.get((run["id"], challenge["id"]))
            if plan.role == "primary" and prestarted is not None and not prestarted.done():
                continue
            if plan.role == "primary":
                if plan.needs_instance:
                    if not self._supports_instance_lifecycle(platform):
                        warning_key = (run["id"], challenge["id"])
                        if warning_key not in self._instance_capability_warnings:
                            self._instance_capability_warnings.add(warning_key)
                            self.store.append_run_event(
                                run["id"], "instance.lifecycle_unsupported",
                                {
                                    "challenge_id": challenge["id"],
                                    "external_id": challenge["external_id"],
                                    "reason": "platform does not expose start/renew/stop lifecycle",
                                },
                            )
                        continue
                    if not self._start_instance(run, challenge):
                        continue
                if self.store.find_assignment(run["id"], challenge["id"], plan.member) is None:
                    try:
                        assignment = self.store.assign(
                            run["id"], challenge["id"], plan.member, self.owner,
                            role="primary",
                        )
                    except CompetitionConflict:
                        continue
                    self.store.append_run_event(
                        run["id"], "assignment.started",
                        {
                            "challenge_id": challenge["id"],
                            "member": plan.member,
                            "role": "primary",
                            "session_id": assignment["session_id"],
                        },
                    )
                    # A retried challenge must land on the intent it left open
                    # when it was parked; a first attempt gets its intent from
                    # the Diamond bootstrap path as before.
                    if int((challenge.get("metadata") or {}).get("attempts") or 0) > 0:
                        self._ensure_primary_intent(
                            run, challenge, project_id, plan.member
                        )
                    freed = self._slot_free_at.pop(challenge["id"], None)
                    if freed is not None:
                        self.store.append_run_event(
                            run["id"], "slot.turnover_ms",
                            {"challenge_id": challenge["id"],
                             "ms": int((time.monotonic() - freed) * 1000)},
                        )
            elif plan.role == "helper":
                if self.store.find_assignment(run["id"], challenge["id"], plan.member) is None:
                    try:
                        assignment = self.store.assign(
                            run["id"], challenge["id"], plan.member, self.owner,
                            role="helper",
                        )
                    except CompetitionConflict:
                        continue
                    self.store.append_run_event(
                        run["id"], "assignment.started",
                        {
                            "challenge_id": challenge["id"],
                            "member": plan.member,
                            "role": "helper",
                            "session_id": assignment["session_id"],
                        },
                    )
                self._ensure_helper_intent(run, challenge, project_id, plan.member)
                if (challenge.get("metadata") or {}).get("remote"):
                    self._publish_instance_entries(run)
                try:
                    self._starting_members.add(start_key)
                    if project_id not in self._starting_projects:
                        self._starting_projects.add(project_id)
                        orchestrator.start_project_async(project_id)
                except Exception as exc:
                    _recovery_logger.debug('Error handled in _schedule_projects', exc_info=True)
                    self._starting_members = {
                        key for key in self._starting_members if key[0] != project_id
                    }
                    self._starting_projects.discard(project_id)
                    self.store.append_run_event(
                        run["id"], "assignment.helper_start_failed",
                        {"challenge_id": challenge["id"], "member": plan.member, "error": str(exc)},
                    )
                continue
            if (challenge.get("metadata") or {}).get("remote"):
                self._publish_instance_entries(run)
            self._starting_members.add(start_key)
            self._starting_projects.add(project_id)
            try:
                orchestrator.start_project_async(project_id)
            except Exception as exc:
                _recovery_logger.debug('Error handled in _schedule_projects', exc_info=True)
                self._starting_members = {
                    key for key in self._starting_members if key[0] != project_id
                }
                self._starting_projects.discard(project_id)
                self.store.append_run_event(
                    run["id"], "assignment.start_failed",
                    {"challenge_id": challenge["id"], "error": str(exc)},
                )

    def _ensure_helper_intent(
        self, run: dict, challenge: dict, project_id: str, member: str
    ) -> str | None:
        """Give a freshly seated helper an intent it alone can work on.

        The orchestrator dispatches Members per open intent, and the seat fence
        only lets a seated Member run. A helper seat with no intent therefore
        produced a live seat with an idle session; this closes that gap by
        creating one intent owned by that Member.

        The direction comes from the primary's most recent report, so the
        helper pursues a stated alternative rather than redoing the main line.
        Without a report it is given an explicit cross-check brief.
        """

        from backend.blackboard import edge_store, graph_store, node_store

        try:
            with self.state.db.connect() as connection:
                detail = graph_store.project_detail(connection, project_id)
                if detail is None:
                    return None
                for intent in detail.intents:
                    if intent.to is None and intent.worker == member:
                        return intent.id
                reports = sorted(
                    (report for report in detail.reports if report.member != member),
                    key=lambda report: report.created_at,
                )
                direction = ""
                start_node = "origin"
                if reports:
                    latest = reports[-1]
                    candidates = [text.strip() for text in (latest.directions or []) if text.strip()]
                    taken = {
                        edge_store.normalize_intent_description(item.description)
                        for item in detail.intents
                        if item.to is None
                    }
                    for candidate in candidates:
                        if edge_store.normalize_intent_description(candidate) not in taken:
                            direction = candidate
                            break
                    if latest.node_id and node_store.fact_exists(
                        connection, project_id, latest.node_id
                    ):
                        start_node = latest.node_id
                if not direction:
                    direction = (
                        "Helper contract: the primary owns the main exploit/solver. "
                        "Read the blackboard and /workspace/shared handoffs first. "
                        "Own one complementary subtask: verify a claimed property or test a distinct fallback hypothesis. "
                        "Do not independently redo the full primary analysis. Deliver concrete evidence and a runnable "
                        "artifact under /workspace/shared/<member>/<task>/ with its SHA-256 and run command. "
                        "Use publish_fact for verified milestones and send_message to notify the primary of the "
                        "handoff or a blocker; reply_to identifies the request being addressed. "
                        "Finish when that subtask is verified or its failure is evidenced."
                    )
                existing = edge_store.find_similar_open_intent(
                    connection, project_id, [start_node], direction
                )
                if existing is not None and existing.worker is None:
                    edge_store.claim_intent(connection, project_id, existing.id, member)
                    intent_id = existing.id
                elif existing is not None:
                    return existing.id
                else:
                    intent = edge_store.create_intent(
                        connection, project_id, [start_node], direction,
                        "diamond", worker=member,
                    )
                    intent_id = intent.id
                graph_store.add_agent(
                    connection, project_id, member, "member",
                    state="active", start_fact_id=start_node,
                )
                graph_store.add_link(connection, project_id, "diamond", member, "assign")
                graph_store.add_link(
                    connection, project_id, member, f"intent:{intent_id}", "explore"
                )
        except Exception as exc:
            _recovery_logger.debug('Error handled in _ensure_helper_intent', exc_info=True)
            self.store.append_run_event(
                run["id"], "assignment.helper_intent_failed",
                {
                    "challenge_id": challenge["id"],
                    "member": member,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return None
        self.store.append_run_event(
            run["id"], "assignment.helper_intent",
            {
                "challenge_id": challenge["id"],
                "member": member,
                "intent_id": intent_id,
                "source": "report_direction" if reports else "cross_check_template",
            },
        )
        return intent_id

    def _ensure_primary_intent(
        self, run: dict, challenge: dict, project_id: str, member: str
    ) -> str | None:
        """Continue the parked project's graph instead of minting a new intent.

        A park keeps the project, intents, session and artifacts. When the same
        challenge is assigned again, the Member's seat must point at the open
        intent it was working on (or the oldest unclaimed one) so the resumed
        session keeps its thread. Creating a fresh intent every time scattered
        the retry across disconnected graph branches.
        """

        from backend.blackboard import edge_store, graph_store

        availability = "reused"
        try:
            with self.state.db.connect() as connection:
                detail = graph_store.project_detail(connection, project_id)
                if detail is None:
                    return None
                open_intents = [intent for intent in detail.intents if intent.to is None]
                same_worker = next(
                    (intent for intent in open_intents if intent.worker == member), None
                )
                if same_worker is not None:
                    intent_id = same_worker.id
                else:
                    unclaimed = next(
                        (intent for intent in open_intents if intent.worker is None), None
                    )
                    start_node = next(
                        (fact.id for fact in reversed(detail.facts) if fact.id != "goal"),
                        "origin",
                    )
                    if unclaimed is not None:
                        edge_store.claim_intent(connection, project_id, unclaimed.id, member)
                        intent_id = unclaimed.id
                        availability = "claimed"
                    else:
                        intent = edge_store.create_intent(
                            connection,
                            project_id,
                            [start_node],
                            (
                                f"第 {int((challenge.get('metadata') or {}).get('attempts') or 1)} 次接手："
                                "这是同一挑战的再次尝试。先读 【park 交接】事实与 "
                                "recent_observations，再选一条尚未排除的方向继续；不要重复已排除的命令。"
                            ),
                            "diamond",
                            worker=member,
                        )
                        intent_id = intent.id
                        availability = "created"
                    graph_store.add_agent(
                        connection, project_id, member, "member",
                        state="active", start_fact_id=start_node,
                    )
                    if not graph_store.link_exists(connection, project_id, "diamond", member, "assign"):
                        graph_store.add_link(connection, project_id, "diamond", member, "assign")
                    if not graph_store.link_exists(
                        connection, project_id, member, f"intent:{intent_id}", "explore"
                    ):
                        graph_store.add_link(
                            connection, project_id, member, f"intent:{intent_id}", "explore"
                        )
        except Exception as exc:
            _recovery_logger.debug("Error handled in _ensure_primary_intent", exc_info=True)
            with suppress(Exception):
                self.store.append_run_event(
                    run["id"], "assignment.primary_intent_failed",
                    {"challenge_id": challenge["id"], "member": member,
                     "error": f"{type(exc).__name__}: {exc}"},
                )
            return None
        self.store.append_run_event(
            run["id"], "assignment.primary_intent",
            {"challenge_id": challenge["id"], "member": member,
             "intent_id": intent_id, "source": availability},
        )
        return intent_id

    def _restore_paused_seats(self, run: dict, rows: list[dict]) -> None:
        """Re-seat paused Members on their original challenges.

        Pausing releases every durable seat.  A positional re-plan after
        resume would scatter Members onto challenges where they own no
        session, so the released seat mapping is restored first; the
        planner only fills seats that could not be restored.
        """
        by_id = {item["id"]: item for item in rows}
        configured = {member.name for member in self.state.config.available_members()}
        taken: set[str] = set()
        for seat in self.store.restorable_seats(run["id"]):
            challenge = by_id.get(seat["challenge_id"])
            if challenge is None or challenge["state"] not in {"ready", "solving"}:
                continue
            deadline_at = _timestamp(challenge.get("deadline_at"))
            if deadline_at is not None and deadline_at <= datetime.now(timezone.utc):
                continue
            member = seat["member"]
            if member in taken or member not in configured:
                continue
            try:
                self.store.assign(
                    run["id"], challenge["id"], member, self.owner, role=seat["role"]
                )
            except CompetitionConflict:
                continue
            taken.add(member)
            self.store.append_run_event(
                run["id"], "assignment.restored",
                {"challenge_id": challenge["id"], "member": member, "role": seat["role"]},
            )

    @staticmethod
    def _resolve_remote_limit(configured_spec: dict, platform: Any, run_limits=None) -> tuple[int, str]:
        """Share the same precedence between preflight, run snapshots and ticks."""
        run_limits = run_limits or {}
        if isinstance(run_limits, str):
            try:
                run_limits = json.loads(run_limits)
            except (TypeError, ValueError):
                run_limits = {}
        override = run_limits.get("remote_concurrency") if isinstance(run_limits, dict) else None
        configured = override if override is not None else configured_spec.get("remote_concurrency", 0)
        if override is not None or configured:
            return max(0, int(configured)), "run" if override is not None else "workflow.remote_concurrency"
        reported = None
        reader = getattr(platform, "instance_limit", None)
        if callable(reader):
            try:
                reported = reader()
            except Exception:
                _recovery_logger.debug('Error handled in _resolve_remote_limit', exc_info=True)
                reported = None
        if reported is not None:
            return max(0, int(reported)), "platform.reported"
        if configured_spec.get("remote_instance_limit"):
            return int(configured_spec["remote_instance_limit"]), "workflow.remote_instance_limit"
        return (1, "platform.default") if CompetitionService._supports_instance_lifecycle(platform) else (0, "platform.unsupported")

    def _remote_instance_limit(self, run: dict, platform: Any) -> int:
        limit, source = self._resolve_remote_limit(run["config_snapshot"]["workflow_spec"], platform, run.get("limits"))
        if source != "platform.default":
            return limit
        warning_key = (run["id"], "instance_limit_default")
        if warning_key not in self._instance_capability_warnings:
            self._instance_capability_warnings.add(warning_key)
            self.store.append_run_event(
                run["id"], "instance.limit_defaulted",
                {
                    "limit": 1,
                    "reason": "platform reported no per-team container limit and "
                              "the workflow sets no remote_instance_limit",
                },
            )
        return limit

    def _local_task_limit(self, run: dict) -> int:
        """Read the mutable local capacity on every scheduler tick."""
        limits = run.get("limits") or {}
        if isinstance(limits, str):
            try:
                limits = json.loads(limits)
            except (TypeError, ValueError):
                limits = {}
        value = limits.get("max_concurrent_tasks") if isinstance(limits, dict) else None
        if value is None:
            value = self.state.config.limits.max_concurrent_tasks
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return max(0, int(self.state.config.limits.max_concurrent_tasks))

    def _instance_ready(self, challenge_id: str) -> bool:
        with self.state.db.connect() as connection:
            row = connection.execute(
                "SELECT state FROM competition_instances WHERE challenge_id=%s",
                (challenge_id,),
            ).fetchone()
        return bool(row and row["state"] == "ready")

    @staticmethod
    def _supports_instance_lifecycle(platform: Any) -> bool:
        """Return whether this adapter can own a remote instance safely.

        ``CompetitionPlatform`` publishes an explicit capability, while the
        deterministic fixture and older adapters only expose methods.  A
        capability is considered usable only when a real start method exists;
        treating a read-only ``instances()`` endpoint as writable would reserve
        a seat forever and repeatedly retry a side effect that cannot succeed.
        """
        declared = getattr(platform, "supports_instances", None)
        if declared is False:
            return False
        return callable(getattr(platform, "start_instance", None))

    @staticmethod
    def _instance_next_renewal(value: Any, *, default_seconds: int = 300) -> datetime:
        """Calculate a bounded renewal point from a platform response."""
        now = datetime.now(timezone.utc)
        if isinstance(value, dict):
            for key in ("renew_after", "renewAfter", "ttl_seconds", "ttlSeconds"):
                raw = value.get(key)
                try:
                    seconds = max(5, min(3600, int(float(raw))))
                except (TypeError, ValueError):
                    continue
                return now + timedelta(seconds=seconds)
            for key in ("expires_at", "expiresAt", "expiration", "expireAt"):
                raw = value.get(key)
                if raw:
                    try:
                        expiry = _timestamp(raw)
                    except (TypeError, ValueError):
                        expiry = None
                    if expiry is not None:
                        remaining = max(5, int((expiry - now).total_seconds()))
                        margin = value.get("renew_before_seconds")
                        if isinstance(margin, (int, float)) and margin > 0:
                            return now + timedelta(seconds=max(5, remaining - margin))
                        return now + timedelta(
                            seconds=max(5, min(3600, remaining // 2))
                        )
        return now + timedelta(seconds=default_seconds)

    def _publish_instance_entries(self, run: dict) -> None:
        """Push each live instance entry to its seated Members over recon.

        The project facts only carry the platform list URL, so without this a
        container challenge leaves the Member guessing its target — observed
        behaviour was Members probing the platform host itself.  Entries change
        on every rebuild, so the notice is republished whenever the stored
        entry differs from what this session last heard.
        """
        with self.state.db.connect() as connection:
            instances = connection.execute(
                """SELECT i.challenge_id,i.external_id,i.metadata,c.instance_generation
                   FROM competition_instances i JOIN competition_challenges c ON c.id=i.challenge_id
                   WHERE i.run_id=%s AND i.state IN ('ready','running','active')""",
                (run["id"],),
            ).fetchall()
        if not instances:
            return
        now = datetime.now(timezone.utc)
        for instance in instances:
            metadata = instance.get("metadata") or {}
            entry = metadata.get("entry") or metadata.get("url")
            if not entry:
                continue
            for assignment in self.store.assignments(run["id"], active_only=True):
                if assignment["challenge_id"] != instance["challenge_id"]:
                    continue
                if assignment["role"] == "wp":
                    continue
                generation = int(instance["instance_generation"])
                key = (instance["challenge_id"], assignment["session_id"], str(entry), generation)
                if key in self._published_instance_entries:
                    continue
                message = Envelope(
                    run_id=run["id"],
                    challenge_id=instance["challenge_id"],
                    sender_member_id=SERVICE_MEMBER_ID,
                    receiver_member_id=assignment["member"],
                    sender_session_id=assignment["session_id"],
                    receiver_session_id=assignment["session_id"],
                    request_id=(
                        f"target-instance:{instance['challenge_id']}:{assignment['session_id']}"
                        f":g{generation}:e{int(assignment['epoch'])}:"
                        f"{hashlib.sha256(str(entry).encode()).hexdigest()[:12]}"
                    ),
                    correlation_id=f"target-instance:{instance['challenge_id']}",
                    lease_epoch=int(assignment["epoch"]),
                    created_at=now,
                    deadline_at=now + timedelta(hours=6),
                    message_type="recon",
                    payload={
                        "kind": "target_instance",
                        "external_id": instance["external_id"],
                        "entry": str(entry),
                        "instance_generation": generation,
                        "instruction": (
                            "This challenge's live target is the per-team instance at this entry. "
                            "Attack only this address. Never probe, scan, or submit to the "
                            "competition platform itself."
                        ),
                    },
                )
                try:
                    self.publish_recon(message)
                    self._published_instance_entries.add(key)
                except Exception as exc:
                    _recovery_logger.debug('Error handled in _publish_instance_entries', exc_info=True)
                    self.store.append_run_event(
                        run["id"], "instance.entry_publish_failed",
                        {"challenge_id": instance["challenge_id"], "error": str(exc)},
                    )

    def _repair_assigned_instances(self, run: dict) -> None:
        """Repair an expired target that still belongs to an admitted solver."""
        assigned = {row["challenge_id"] for row in self.store.assignments(run["id"], active_only=True) if row["role"] != "wp"}
        with self.state.db.connect() as connection:
            expired = {row["challenge_id"] for row in connection.execute(
                """SELECT challenge_id FROM competition_instances WHERE run_id=%s AND state='expired'
                   AND metadata->>'repair_required'='true'""", (run["id"],),
            ).fetchall()}
        for challenge in self.store.challenges(run["id"]):
            if (
                challenge["id"] in assigned & expired
                and challenge["state"] == "solving"
                and self._start_instance(run, challenge)
            ):
                self.store.append_run_event(run["id"], "instance.repaired", {"challenge_id": challenge["id"]})

    def _renew_instances(self, run: dict) -> None:
        platform = self._platform_for_run(run)
        with self.state.db.connect() as connection:
            due = connection.execute(
                """SELECT * FROM competition_instances
                   WHERE run_id=%s AND owned=true AND state='ready'
                   AND next_renew_at IS NOT NULL AND next_renew_at<=now()""",
                (run["id"],),
            ).fetchall()
        for instance in due:
            self.store.append_run_event(run["id"], "instance.renew_requested", {"challenge_id": instance["challenge_id"]})
            try:
                renewed = platform.renew_instance(instance["external_id"])
            except Exception as exc:
                _recovery_logger.debug('Error handled in _renew_instances', exc_info=True)
                message = str(exc).casefold()
                reason = getattr(exc, "reason", "")
                if reason in {"expired", "missing"} or (not reason and any(
                    token in message for token in ("not created", "does not exist", "expired", "not found")
                )):
                    with self.state.db.connect() as connection:
                        connection.execute(
                            """UPDATE competition_instances SET state='expired',next_renew_at=NULL,
                               metadata=metadata || %s,updated_at=now() WHERE challenge_id=%s""",
                            (Jsonb({"renew_error": str(exc), "repair_required": True,
                                    "repair_after": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}), instance["challenge_id"]),
                        )
                    self.store.append_run_event(
                        run["id"], "instance.expired",
                        {"challenge_id": instance["challenge_id"], "reason": reason or "unavailable",
                         "renew_error": str(exc), "external_id": instance["external_id"]},
                    )
                    continue
                try:
                    retry_after = max(5, min(3600, int(getattr(exc, "retry_after", 60))))
                except (TypeError, ValueError):
                    retry_after = 60
                with self.state.db.connect() as connection:
                    connection.execute(
                        """UPDATE competition_instances SET next_renew_at=now()+(%s*interval '1 second'),
                           metadata=metadata || %s,updated_at=now() WHERE challenge_id=%s""",
                        (retry_after, Jsonb({"renew_error": str(exc), "renew_error_kind": reason or "transient"}),
                         instance["challenge_id"]),
                    )
                self.store.append_run_event(
                    run["id"], "instance.renew_failed",
                    {"challenge_id": instance["challenge_id"], "external_id": instance["external_id"],
                     "error": str(exc), "reason": reason or "transient", "retry_after": retry_after},
                )
                continue
            next_renewal = self._instance_next_renewal(renewed)
            with self.state.db.connect() as connection:
                if isinstance(renewed, dict):
                    connection.execute(
                        """UPDATE competition_instances SET metadata=%s,
                           next_renew_at=%s,updated_at=now() WHERE challenge_id=%s""",
                        (Jsonb(renewed), next_renewal, instance["challenge_id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE competition_instances SET next_renew_at=%s,
                           updated_at=now() WHERE challenge_id=%s""",
                        (next_renewal, instance["challenge_id"]),
                    )
            self.store.append_run_event(run["id"], "instance.renewed", {"challenge_id": instance["challenge_id"]})

    def _instance_slot_available(self, run: dict, challenge: dict, platform) -> bool:
        """Re-check the platform quota immediately before starting a container.

        Lookahead and the tick planner compute capacity independently. Between
        planning and the platform call another start can land, and TSecBench
        answers 409 "max active"; checking here turns that race into a cheap
        deferral instead of a failed start.
        """

        with suppress(Exception):
            limit = int(self._remote_instance_limit(run, platform) or 0)
            if limit <= 0:
                return True
            with self.state.db.connect() as connection:
                row = connection.execute(
                    """SELECT COUNT(DISTINCT i.challenge_id) AS used
                       FROM competition_instances i
                       JOIN competition_run_challenges rc ON rc.challenge_id=i.challenge_id
                       WHERE rc.run_id=%s
                         AND i.state IN ('ready','running','active','start_pending','stop_pending')""",
                    (run["id"],),
                ).fetchone()
            return int((row or {}).get("used") or 0) < limit
        return True

    def _try_reuse_instance(
        self, run: dict, challenge: dict, platform, existing: dict
    ) -> bool:
        """Renew a parked container before paying for a fresh one.

        A parked challenge keeps its competition_instances row on purpose. When
        the platform still owns the container, renewing it saves the cold start;
        when renewal fails because the box is gone, the caller falls through to
        the normal start/rebuild path.
        """

        renew = getattr(platform, "renew_instance", None)
        if not callable(renew):
            return False
        started = time.monotonic()
        try:
            result = renew(challenge["external_id"])
        except Exception:
            _recovery_logger.debug("instance renew failed; falling back to start", exc_info=True)
            return False
        if not isinstance(result, dict):
            return False
        state = str(result.get("state", "ready")).strip().lower()
        if state not in {"ready", "running", "active"}:
            return False
        result.setdefault("state", "ready")
        next_renewal = self._instance_next_renewal(result)
        try:
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances
                       SET state='ready',
                           metadata = COALESCE(metadata,'{}'::jsonb) || %s,
                           next_renew_at=%s, updated_at=now()
                       WHERE challenge_id=%s""",
                    (Jsonb({**result, "reused": True}), next_renewal, challenge["id"]),
                )
        except Exception:
            _recovery_logger.debug("instance reuse registration failed", exc_info=True)
            return False
        self.store.append_run_event(
            run["id"], "instance.reused",
            {"challenge_id": challenge["id"],
             "reuse_ms": int((time.monotonic() - started) * 1000),
             "previous_state": existing.get("state")},
        )
        return True

    def _start_instance(self, run: dict, challenge: dict) -> bool:
        platform = self._platform_for_run(run)
        if not self._supports_instance_lifecycle(platform):
            return False
        with self.state.db.connect() as connection:
            existing = connection.execute(
                "SELECT state,metadata FROM competition_instances WHERE challenge_id=%s",
                (challenge["id"],),
            ).fetchone()
        if existing is not None and existing["state"] == "expired":
            repair_after = (existing.get("metadata") or {}).get("repair_after")
            if repair_after:
                try:
                    if _timestamp(repair_after) and _timestamp(repair_after) > datetime.now(timezone.utc):
                        return False
                except (TypeError, ValueError):
                    pass
        if existing is not None and existing["state"] in {
            "ready", "running", "active", "start_pending", "stop_pending"
        }:
            return existing["state"] in {"ready", "running", "active"}
        if not self._instance_slot_available(run, challenge, platform):
            self.store.append_run_event(
                run["id"], "instance.quota_deferred",
                {"challenge_id": challenge["id"],
                 "reason": "platform active-instance limit already occupied"},
            )
            return False
        if (
            existing is not None
            and existing["state"] == "stopped"
            and self._try_reuse_instance(run, challenge, platform, existing)
        ):
            return True
        started = time.monotonic()
        try:
            self.store.append_run_event(run["id"], "instance.start_requested", {"challenge_id": challenge["id"]})
            result = platform.start_instance(challenge["external_id"])
        except Exception as exc:
            _recovery_logger.debug('Error handled in _start_instance', exc_info=True)
            if self._is_platform_closed(exc):
                self._note_platform_closed(run["id"], exc)
            with self.state.db.connect() as connection:
                connection.execute(
                    """INSERT INTO competition_instances
                       (challenge_id,run_id,external_id,state,metadata,updated_at)
                       VALUES (%s,%s,%s,'expired',%s,now())
                       ON CONFLICT (challenge_id) DO UPDATE SET state='expired',
                         metadata=competition_instances.metadata || EXCLUDED.metadata,
                         updated_at=now()""",
                    (challenge["id"], run["id"], challenge["external_id"],
                     Jsonb({"start_error": str(exc), "repair_after": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()})),
                )
            self.store.append_run_event(
                run["id"], "instance.start_failed",
                {"challenge_id": challenge["id"], "error": str(exc)},
            )
            return False
        start_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(result, dict):
            result = {"state": "ready"}
        state = str(result.get("state", "ready")).strip().lower()
        if state not in {"ready", "running", "active"}:
            with self.state.db.connect() as connection:
                connection.execute(
                    """INSERT INTO competition_instances
                       (challenge_id,run_id,external_id,state,metadata,updated_at)
                       VALUES (%s,%s,%s,'start_pending',%s,now())
                       ON CONFLICT (challenge_id) DO UPDATE SET state='start_pending',
                       metadata=EXCLUDED.metadata,updated_at=now()""",
                    (challenge["id"], run["id"], challenge["external_id"], Jsonb(result)),
                )
            self.store.append_run_event(
                run["id"], "instance.start_pending",
                {"challenge_id": challenge["id"], "state": state or "unknown",
                 "start_ms": start_ms},
            )
            return False
        result.setdefault("state", "ready")
        next_renewal = self._instance_next_renewal(result)
        try:
            with self.state.db.connect() as connection:
                connection.execute(
                    """INSERT INTO competition_instances
                       (challenge_id,run_id,external_id,state,metadata,next_renew_at)
                       VALUES (%s,%s,%s,'ready',%s,%s)
                       ON CONFLICT (challenge_id) DO UPDATE SET state='ready',metadata=EXCLUDED.metadata,
                       next_renew_at=EXCLUDED.next_renew_at,updated_at=now()""",
                    (
                        challenge["id"], run["id"], challenge["external_id"],
                        Jsonb(result), next_renewal,
                    ),
                )
                connection.execute(
                    "UPDATE competition_challenges SET instance_generation=instance_generation+1 WHERE id=%s",
                    (challenge["id"],),
                )
        except Exception as exc:
            _recovery_logger.debug('Error handled in _start_instance', exc_info=True)
            try:
                with self.state.db.connect() as connection:
                    connection.execute(
                        """INSERT INTO competition_instances
                           (challenge_id,run_id,external_id,state,metadata,updated_at)
                           VALUES (%s,%s,%s,'stop_pending',%s,now())
                           ON CONFLICT (challenge_id) DO UPDATE SET state='stop_pending',
                           metadata=EXCLUDED.metadata,updated_at=now()""",
                        (
                            challenge["id"], run["id"], challenge["external_id"],
                            Jsonb({"registration_error": str(exc)}),
                        ),
                    )
            finally:
                self.store.append_run_event(
                    run["id"], "instance.registration_failed",
                    {"challenge_id": challenge["id"], "error": str(exc)},
                )
            return False
        self.store.append_run_event(
            run["id"], "instance.started",
            {"challenge_id": challenge["id"], "start_ms": start_ms,
             "repair": bool(existing and existing["state"] == "expired")},
        )
        return True

    def _process_submissions(self, run: dict, *, allow_new: bool = True) -> None:
        platform = self._platform_for_run(run)
        self._ingest_project_candidates(run)
        with self.state.db.connect() as connection:
            closeout_rows = connection.execute(
                """SELECT s.*,c.external_id,c.project_id FROM competition_submissions s
                   JOIN competition_challenges c ON c.id=s.challenge_id
                   LEFT JOIN projects p ON p.id=c.project_id
                   WHERE s.run_id=%s AND s.status='correct' AND p.id IS NOT NULL
                     AND COALESCE((s.verdict->>'challenge_complete')::boolean, true)
                     AND (p.status IS DISTINCT FROM 'solved'
                          OR EXISTS (SELECT 1 FROM competition_assignments a
                                     WHERE a.challenge_id=c.id AND a.released_at IS NULL))
                   ORDER BY s.created_at""",
                (run["id"],),
            ).fetchall()
        for submission in closeout_rows:
            challenge = next(
                item for item in self.store.challenges(run["id"])
                if item["id"] == submission["challenge_id"]
            )
            self._on_correct(run, challenge, submission["candidate"])
        with self.state.db.connect() as connection:
            unresolved = connection.execute(
                """SELECT s.*,c.external_id FROM competition_submissions s
                   JOIN competition_challenges c ON c.id=s.challenge_id
                   WHERE s.run_id=%s AND s.status IN ('pending','unknown')
                   AND s.platform_submission_id IS NOT NULL ORDER BY s.created_at""",
                (run["id"],),
            ).fetchall()
        for submission in unresolved:
            try:
                result = platform.query(
                    submission["external_id"], submission["platform_submission_id"]
                )
            except Exception:
                _recovery_logger.debug('Error handled in _process_submissions', exc_info=True)
                continue
            finished = self.store.finish_submission(submission["id"], result)
            self.store.append_run_event(
                run["id"], "submission.verdict",
                {"challenge_id": submission["challenge_id"], "submission_id": submission["id"],
                 "verdict": finished["status"], "source": "query"},
            )
            if finished["status"] == "correct" and (finished.get("verdict") or {}).get("challenge_complete", True):
                challenge = next(
                    item for item in self.store.challenges(run["id"])
                    if item["id"] == submission["challenge_id"]
                )
                self._on_correct(run, challenge, submission["candidate"])
        if not allow_new:
            return
        while not self._stop.is_set():
            submission = self.store.claim_submission(run["id"])
            if submission is None:
                break
            challenge = next(
                item for item in self.store.challenges(run["id"])
                if item["id"] == submission["challenge_id"]
            )
            try:
                result = platform.submit(challenge["external_id"], submission["candidate"])
            except Exception:
                _recovery_logger.debug('Error handled in _process_submissions', exc_info=True)
                result = {"verdict": "unknown"}
            finished = self.store.finish_submission(submission["id"], result)
            self.store.append_run_event(
                run["id"], "submission.verdict",
                {"challenge_id": challenge["id"], "submission_id": submission["id"],
                 "verdict": finished["status"]},
            )
            if finished["status"] == "correct" and (finished.get("verdict") or {}).get("challenge_complete", True):
                self._on_correct(run, challenge, submission["candidate"])
                self._mark_project_candidate(
                    challenge, submission["candidate"], "verified"
                )
            elif finished["status"] == "correct":
                self._mark_project_candidate(challenge, submission["candidate"], "verified")
                self.store.append_run_event(run["id"], "challenge.flag_progress",
                                            {"challenge_id": challenge["id"], "progress": finished.get("verdict")})
                orchestrator = self.state.orchestrator
                if challenge.get("project_id") and orchestrator is not None:
                    with suppress(Exception):
                        orchestrator._resume_after_verdict(challenge["project_id"], reason="flag accepted; remaining platform objectives are incomplete")
            elif finished["status"] in {"wrong", "rejected"}:
                self._mark_project_candidate(
                    challenge, submission["candidate"], "rejected"
                )
                orchestrator = self.state.orchestrator
                if challenge.get("project_id") and orchestrator is not None:
                    with suppress(Exception):
                        orchestrator._resume_after_verdict(
                            challenge["project_id"], reason="platform rejected candidate"
                        )
            elif finished["status"] in {"unknown", "auth_required"}:
                self._mark_project_candidate(
                    challenge, submission["candidate"], "unknown"
                )
            if finished["status"] in {"pending", "unknown", "auth_required"}:
                break

    def _ingest_project_candidates(self, run: dict) -> None:
        with self.state.db.connect() as connection:
            rows = connection.execute(
                """SELECT f.id,f.normalized_flag,f.evidence_artifact,c.id AS challenge_id
                   FROM flag_submissions f
                   JOIN competition_challenges c ON c.project_id=f.project_id
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id=%s AND f.status='pending'
                   ORDER BY f.id""",
                (run["id"],),
            ).fetchall()
        for row in rows:
            try:
                self.store.queue_recovered_candidate(
                    run["id"], row["challenge_id"], row["normalized_flag"],
                    row.get("evidence_artifact") or "candidate produced by the assigned Member session",
                )
            except CompetitionConflict:
                continue

    def _mark_project_candidate(self, challenge: dict, flag: str, status: str) -> None:
        project_id = challenge.get("project_id")
        if not project_id:
            return
        with self.state.db.connect() as connection:
            connection.execute(
                """UPDATE flag_submissions SET status=%s,
                   verified_at=CASE WHEN %s='verified' THEN now() ELSE verified_at END
                   WHERE project_id=%s AND normalized_flag=%s
                   AND status IN ('pending','judging','unknown','error')""",
                (status, status, project_id, flag.strip()),
            )

    def _on_correct(self, run: dict, challenge: dict, flag: str) -> None:
        with self.state.db.connect() as connection:
            partial = connection.execute(
                """SELECT verdict FROM competition_submissions WHERE run_id=%s AND challenge_id=%s
                   AND candidate=%s AND status='correct' ORDER BY created_at DESC LIMIT 1""",
                (run["id"], challenge["id"], flag),
            ).fetchone()
        if partial and (partial.get("verdict") or {}).get("challenge_complete") is False:
            return
        project_id = challenge.get("project_id")
        orchestrator = self.state.orchestrator
        if project_id:
            try:
                with self.state.db.connect() as connection:
                    self.store._lock(connection, run["id"])
                    connection.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
                    goal = connection.execute(
                        "SELECT 1 FROM intents WHERE project_id=%s AND to_fact_id='goal' LIMIT 1",
                        (project_id,),
                    ).fetchone()
                    if goal is None:
                        fact = node_store.reserve_fact(
                            connection, project_id,
                            "Verified flag submitted through the competition closeout API.",
                        )
                        node_store.insert_fact(connection, project_id, fact.id, fact.description)
                        edge_store.complete_goal_intent(
                            connection, project_id, [fact.id],
                            "verified flag submission", SERVICE_MEMBER_ID,
                        )
                    accept_verified_flag(
                        connection, project_id, flag,
                        source=f"competition:{run['workflow_id']}",
                    )
                    from backend.core.postprocess_store import (
                        enqueue_postprocess,
                    )

                    enqueue_postprocess(connection, project_id)
                    self.store.finalize_correct_challenge(connection, run["id"], challenge["id"])
            except Exception as exc:
                _recovery_logger.debug('Error handled in _on_correct', exc_info=True)
                self.store.append_run_event(
                    run["id"], "challenge.closeout_failed",
                    {"challenge_id": challenge["id"], "error": str(exc)},
                )
                with suppress(Exception):
                    current = self.store.get_run(run["id"])
                    if current["status"] == "running":
                        self.store.transition_run(
                            run["id"], "blocked", revision=current["revision"]
                        )
                return
            if orchestrator is not None:
                with suppress(Exception):
                    orchestrator.stop_project(project_id)
            self._release_project_engine(project_id)
        for assignment in self.store.assignments(run["id"], active_only=True):
            if assignment["challenge_id"] == challenge["id"] and assignment["role"] != "wp":
                self.store.release(
                    assignment["id"], assignment["lease_owner"], assignment["epoch"]
                )
        if self._instance_ready(challenge["id"]):
            self._submit_stop(run, challenge)
        self._prestart_lookahead(run)

    def writeups_active(self) -> bool:
        """Hot switch: the WP page toggles writeups without restarting IPC.

        IPC_WP_JOBS=0 still disables WP for a scored benchmark at deploy time;
        the runtime config value is read on every dispatch, so enabling or
        disabling WP in the web UI applies to the very next challenge.
        """

        if not bool(getattr(self.state.config, "wp_enabled", True)):
            return False
        return writeups_enabled()

    def _dispatch_wp(self, run: dict) -> None:
        if not self.writeups_active():
            return
        finished = [key for key, future in self._wp_futures.items() if future.done()]
        for key in finished:
            self._wp_futures.pop(key, None)
        while len(self._wp_futures) < 2:
            claimed = self.store.claim_wp_job(run["id"], self.owner, require_verified=True)
            if claimed is None:
                return
            challenge_id = claimed["job"]["challenge_id"]
            self._wp_futures[challenge_id] = self._wp_pool.submit(
                self._run_wp_job, claimed
            )
            self.store.append_run_event(
                run["id"], "wp.started",
                {"challenge_id": challenge_id, "member": claimed["job"]["member"]},
            )

    def _run_wp_job(self, claimed: dict) -> None:
        job, assignment = claimed["job"], claimed["assignment"]
        challenge = next(
            item for item in self.store.challenges(job["run_id"])
            if item["id"] == job["challenge_id"]
        )
        project_id = challenge.get("project_id")
        if not project_id:
            self.store.finish_wp_job(assignment, error="challenge has no project workspace")
            return
        try:
            config = self.state.config.member
            generator = None
            if config is not None and config.configured and config.api_format != "mock":
                adapter = make_adapter(config, name=job["member"])
                session_events = self.store.events(job["session_id"], limit=500)
                session_context = json.dumps(session_events, ensure_ascii=False)[:40_000]

                def generator(prompt: str) -> str:
                    return adapter.chat(
                        [{"role": "user", "content": (
                            "Continue the original challenge session by producing its final writeup.\n"
                            f"Persisted session events:\n{session_context}\n\n{prompt}"
                        )}],
                        system_prompt=WRITEUP_SYSTEM_PROMPT,
                        temperature=0.1,
                        max_tokens=8192,
                    )

            evidence_logs = {
                kind: self.state.logger.read_log(kind, project_id, limit=160)
                for kind in ("project", "tool", "llm", "memory")
            }
            content, expected_flag = generate_wp_content(
                self.state.db, project_id, generator=generator,
                evidence_logs=evidence_logs,
            )
            with self.state.db.connect() as connection:
                path, _rollback = persist_validated_writeup(
                    connection, project_id, self.state.wp_dir, content,
                    expected_flag=expected_flag,
                )
            self.store.finish_wp_job(assignment, artifact_path=path)
            self.store.append_run_event(
                job["run_id"], "wp.completed",
                {"challenge_id": job["challenge_id"], "member": job["member"],
                 "artifact_path": path},
            )
        except Exception as exc:
            _recovery_logger.debug('Error handled in _run_wp_job', exc_info=True)
            finished_job = None
            with suppress(CompetitionConflict):
                finished_job = self.store.finish_wp_job(assignment, error=str(exc))
            self.store.append_run_event(
                job["run_id"], "wp.failed" if finished_job and finished_job.get("status") == "failed" else "wp.deferred",
                {"challenge_id": job["challenge_id"], "member": job["member"],
                 "error": str(exc)},
            )
