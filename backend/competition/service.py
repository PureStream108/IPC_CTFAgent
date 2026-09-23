from __future__ import annotations

import os
import json
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from psycopg.errors import UndefinedColumn, UndefinedTable

from psycopg.types.json import Jsonb

from backend.competition.platform import CompetitionPlatform
from backend.competition.policy import Challenge, plan_assignments
from backend.competition.store import CompetitionConflict, CompetitionStore
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


TERMINAL_RUN_STATES = {"finished", "stopped"}


def _timestamp(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        sync_interval: int = 120,
        tick_interval: float = 1.0,
        platform_factory: Callable[[str, PlatformWorkflowSpec], Any] | None = None,
        ops_service: OpsAgentService | None = None,
    ):
        self.state = state
        self.store = CompetitionStore(state.db)
        self.sync_interval = max(5, sync_interval)
        self.tick_interval = max(0.1, tick_interval)
        self.owner = f"{state.instance_id}:competition"
        # The legacy Member runtime may still execute a competition Project,
        # but this token identifies the coordinator that owns scheduling it.
        # The orchestrator reads the same token when it bootstraps assigned
        # Members, so a second legacy engine cannot pass the project fence.
        self.engine_owner = self.owner
        state.competition_engine_owner = self.engine_owner
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._starting_projects: set[str] = set()
        # A project can have a primary and a helper assignment.  The legacy
        # orchestrator API is project-scoped, so remember each member request
        # until its durable assignment is visible in the orchestrator.  This
        # prevents the helper branch from enqueueing the same bootstrap on
        # every scheduler tick while a worker is still starting.
        self._starting_members: set[tuple[str, str]] = set()
        self._pair: PairCoordinator | None = None
        self._recon: ReconPublisher | None = None
        self._recon_endpoint: str | None = None
        # The application uses the real declarative platform adapter.  Keeping
        # the constructor seam explicit lets deterministic platforms exercise
        # the same persistent coordinator in acceptance tests and in local
        # replay tools without monkeypatching network clients.
        self._platform_factory = platform_factory
        self._ops_service = ops_service
        self._wp_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ipc-wp")
        self._wp_futures: dict[str, Any] = {}
        # Capability failures are durable run facts, but do not need to be
        # emitted on every scheduler tick while an unsupported remote task is
        # waiting for an operator to change the workflow.
        self._instance_capability_warnings: set[tuple[str, str]] = set()
        self._engine_conflicts: set[tuple[str, str]] = set()
        self.observation_interval = 10.0
        self._last_observation_at = 0.0

    @property
    def ops(self) -> OpsAgentService:
        if self._ops_service is not None:
            return self._ops_service
        service = getattr(self.state, "ops_agent_service", None)
        if service is None:
            service = OpsAgentService(self.state)
            self.state.ops_agent_service = service
        return service

    def start_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="ipc-competition", daemon=True
        )
        self._thread.start()
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
                    # The durable store remains authoritative if a live PUB
                    # socket cannot be bound; subscribers can still replay.
                    self._recon = None
                    self._recon_endpoint = None

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)
        if self._pair is not None:
            self._pair.close()
            self._pair = None
        if self._recon is not None:
            self._recon.close()
            self._recon = None
            self._recon_endpoint = None
        with suppress(Exception):
            self.store.release_engine_leases_for_owner(
                "competition", self.engine_owner
            )
        self._wp_pool.shutdown(wait=True, cancel_futures=True)

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
        workflow = self.ops.store.get_workflow(workflow_id)
        if workflow["status"] != "confirmed" or workflow.get("confirmed_digest") != workflow.get("spec_digest"):
            raise PermissionError("Workflow must be confirmed at its current revision before Start")
        return workflow

    def _platform_for_workflow(self, workflow: dict) -> CompetitionPlatform:
        if self._platform_factory is not None:
            return self._platform_factory(workflow["id"], workflow["spec"])
        return CompetitionPlatform(self.ops, workflow["id"], workflow["spec"])

    def _platform_for_run(self, run: dict) -> CompetitionPlatform:
        snapshot = run["config_snapshot"]
        spec = PlatformWorkflowSpec.model_validate(snapshot["workflow_spec"])
        if self._platform_factory is not None:
            return self._platform_factory(run["workflow_id"], spec)
        return CompetitionPlatform(self.ops, run["workflow_id"], spec)

    def preflight(self, workflow_id: str) -> dict:
        errors = self.state.config.startup_errors()
        if errors:
            raise ValueError("; ".join(errors))
        workflow = self._workflow(workflow_id)
        platform = self._platform_for_workflow(workflow)
        challenges = platform.preflight()
        return {
            "workflow_id": workflow_id,
            "identity_key": platform.identity,
            "challenge_count": len(challenges),
            "remote_count": sum(bool(item.remote) for item in challenges),
            "capabilities": {
                "submit": bool(getattr(
                    platform, "supports_submit",
                    workflow["spec"].submit is not None or getattr(platform, "client", None) is not None,
                )),
                "instances": bool(getattr(
                    platform, "supports_instances", getattr(platform, "client", None) is not None,
                )),
                "async_verdict": bool(
                    workflow["spec"].submit and workflow["spec"].submit.pending_values
                ),
            },
        }

    def start(self, workflow_id: str, idempotency_key: str) -> dict:
        errors = self.state.config.startup_errors()
        if errors:
            raise ValueError("; ".join(errors))
        workflow = self._workflow(workflow_id)
        platform = self._platform_for_workflow(workflow)
        discovered = platform.preflight()
        snapshot = {
            "workflow_spec": workflow["spec"].model_dump(mode="json"),
            "spec_digest": workflow["spec_digest"],
            "member_config": _safe_member_config(self.state.config.member),
            "member_config_source": "runtime-config",
        }
        run = self.store.create_run(
            workflow_id, platform.identity, idempotency_key, snapshot
        )
        if run["status"] != "preflight":
            return self.store.run_snapshot(run["id"])
        self.store.append_run_event(run["id"], "run.created", {"workflow_id": workflow_id})
        run = self.store.transition_run(run["id"], "importing", revision=run["revision"])
        try:
            imported = self.ops._import(
                workflow_id,
                workflow["spec"],
                [item.external_id for item in discovered],
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
        return {
            "title": item.title,
            "category": item.category,
            "description": item.description,
            "attachment_count": len(item.attachment_urls),
            "remote": bool(item.remote),
            "external_solved": bool(item.solved),
            "hints": list(item.hints),
            "ready_at": datetime.now(timezone.utc).isoformat(),
        }

    def control(self, run_id: str, action: str, revision: int) -> dict:
        run = self.store.get_run(run_id)
        if run["revision"] != revision:
            raise CompetitionConflict("stale run revision")
        if action == "refresh":
            self.refresh(run_id)
            return self.store.run_snapshot(run_id)
        target = {"pause": "paused", "resume": "running", "stop": "stopped"}.get(action)
        if target is None:
            raise ValueError("unsupported run action")
        if action in {"pause", "stop"}:
            self._stop_run_projects(run_id)
            self.store.release_run_assignments(run_id)
        if action == "stop":
            self._stop_owned_instances(run)
        changed = self.store.transition_run(run_id, target, revision=revision)
        self.store.append_run_event(run_id, f"run.{target}", {})
        if action == "resume":
            self.wake()
        return self.store.run_snapshot(changed["id"])

    def _stop_run_projects(self, run_id: str) -> None:
        orchestrator = self.state.orchestrator
        for challenge in self.store.challenges(run_id):
            if challenge.get("project_id"):
                if orchestrator is not None:
                    with suppress(Exception):
                        orchestrator.stop_project(challenge["project_id"])
                self._release_project_engine(challenge["project_id"])

    def _claim_project_engine(self, run: dict, project_id: str) -> bool:
        try:
            lease = self.store.claim_engine_lease(
                project_id, "competition", self.engine_owner, seconds=60
            )
        except (UndefinedColumn, UndefinedTable):
            # Keep old deployments readable during the additive migration; a
            # configured database gets the durable fence and cannot silently
            # fall back after the columns are present.
            return True
        except Exception as exc:
            self.store.append_run_event(
                run["id"], "engine.lease_error",
                {"project_id": project_id, "error": str(exc)},
            )
            return False
        if lease is not None:
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
            try:
                renewed = self.store.renew_engine_lease(
                    project_id, "competition", self.engine_owner, seconds=60
                )
            except Exception:
                renewed = True
            if not renewed:
                # A coordinator restart may have allowed an expired lease to
                # be reclaimed; claim_engine_lease also fences that takeover.
                self._claim_project_engine(run, project_id)

    def _release_project_engine(self, project_id: str) -> None:
        with suppress(Exception):
            self.store.release_engine_lease(
                project_id, "competition", self.engine_owner
            )

    def _stop_owned_instances(self, run: dict) -> None:
        platform = self._platform_for_run(run)
        with self.state.db.connect() as connection:
            instances = connection.execute(
                """SELECT * FROM competition_instances
                   WHERE run_id=%s AND owned=true AND state NOT IN ('stopped','expired')""",
                (run["id"],),
            ).fetchall()
        for instance in instances:
            try:
                platform.stop_instance(instance["external_id"])
            except Exception as exc:
                self.store.append_run_event(
                    run["id"], "instance.stop_failed",
                    {"challenge_id": instance["challenge_id"], "error": str(exc)},
                )
                continue
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances SET state='stopped',updated_at=now()
                       WHERE challenge_id=%s""",
                    (instance["challenge_id"],),
                )

    def refresh(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        platform = self._platform_for_run(run)
        discovered = platform.challenges()
        known = {item["external_id"]: item for item in self.store.challenges(run_id)}
        new_items = [item for item in discovered if item.external_id not in known]
        projects: dict[str, str] = {}
        if new_items:
            imported = self.ops._import(
                run["workflow_id"], platform.spec,
                [item.external_id for item in new_items],
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
                   WHERE challenge_id=%s AND status IN ('deferred','failed') RETURNING *""",
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
            except Exception as exc:
                run = self.store.active_run()
                if run:
                    with suppress(Exception):
                        self.store.schedule_next_sync(
                            run["id"], seconds=self.sync_interval, error=str(exc)
                        )
                        self.store.append_run_event(
                            run["id"], "coordinator.error",
                            {"error": f"{type(exc).__name__}: {exc}"},
                        )
            self._wake.wait(self.tick_interval)
            self._wake.clear()

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            run = self.store.active_run()
            if run is None or not self.store.claim_run_lease(run["id"], self.owner):
                return
            if run["status"] in {"running", "paused"}:
                due = _timestamp(run.get("next_sync_at"))
                if due is None or due <= datetime.now(timezone.utc):
                    try:
                        self.refresh(run["id"])
                    except Exception as exc:
                        self.store.schedule_next_sync(
                            run["id"], seconds=self.sync_interval, error=str(exc)
                        )
                        self.store.append_run_event(
                            run["id"], "run.sync_failed", {"error": str(exc)}
                        )
            # Paused runs stop new solver work, but they still have to reconcile
            # already accepted platform submissions and renew instances that
            # are needed to finish an in-flight judge request.  Returning here
            # used to strand pending submissions until Resume and could let a
            # short-lived instance expire while the operator was paused.
            if run["status"] not in {"running", "paused", "draining"}:
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
            # A process can die after the platform accepted a request but
            # before the local row received its submission id.  Preserve that
            # uncertainty and reconcile it through the platform query path;
            # never silently send the candidate again.
            self.store.reconcile_inflight_submissions(run["id"])
            self._stop_expired_challenges(run)
            self.store.heartbeat_run_assignments(run["id"], self.owner)
            self._renew_project_engines(run)
            if run["status"] == "running":
                self._sync_orchestrator_assignments(run)
            self._renew_instances(run)
            self._process_submissions(
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

    def _record_observation(self, run: dict) -> None:
        """Sample durable pressure metrics without making them tick-critical."""
        now = time.monotonic()
        if now - self._last_observation_at < self.observation_interval:
            return
        try:
            self.store.observe_run(run["id"], self.owner)
            self._last_observation_at = now
        except Exception as exc:
            # Observability must not stop judging or release a live lease.  A
            # later tick retries, and the coordinator error stream remains the
            # visible signal if the schema/storage is unavailable.
            self.store.append_run_event(
                run["id"], "observation.failed", {"error": str(exc)}
            )

    def _stop_expired_challenges(self, run: dict) -> None:
        expired_ids = {
            item["id"] for item in self.store.challenges(run["id"])
            if item["state"] == "expired"
        }
        if not expired_ids:
            return
        orchestrator = self.state.orchestrator
        platform = self._platform_for_run(run)
        for challenge in self.store.challenges(run["id"]):
            if challenge["id"] not in expired_ids:
                continue
            if challenge.get("project_id"):
                if orchestrator is not None:
                    with suppress(Exception):
                        orchestrator.stop_project(challenge["project_id"])
                self._release_project_engine(challenge["project_id"])
            if self._instance_ready(challenge["id"]):
                with suppress(Exception):
                    platform.stop_instance(challenge["external_id"])
                with self.state.db.connect() as connection:
                    connection.execute(
                        """UPDATE competition_instances SET state='stopped',updated_at=now()
                           WHERE challenge_id=%s""",
                        (challenge["id"],),
                    )
            self.store.append_run_event(
                run["id"], "challenge.expired", {"challenge_id": challenge["id"]}
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
            with suppress(Exception):
                self._platform_for_run(run).stop_instance(challenge["external_id"])
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances SET state='stopped',updated_at=now()
                       WHERE challenge_id=%s""",
                    (challenge["id"],),
                )

    def _advance_run_completion(self, run: dict) -> None:
        terminal = {"solved", "expired", "withdrawn", "cancelled"}
        challenges = self.store.challenges(run["id"])
        current = self.store.get_run(run["id"])
        if current["status"] == "running" and all(
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
                   WHERE run_id=%s AND status<>'done' LIMIT 1""",
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
            # A live lease still belongs to the previous coordinator.  Do not
            # bootstrap a new Member with that stale fencing tuple; the next
            # tick will reclaim it after the bounded lease timeout.
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
            # During restart the persisted competition assignment is the
            # source of truth while the legacy orchestrator is rebuilding its
            # in-memory Member objects.  Releasing it merely because the
            # process has just restarted would lose the original session and
            # let a different member take the seat.  A concrete conflicting
            # owner, however, is a fencing violation and must be released.
            if owner_project == project_id:
                self._starting_projects.discard(project_id)
                self._starting_members.discard(start_key)
            elif owner_project is not None and owner_project != project_id:
                self._starting_members.discard(start_key)
                self.store.release(
                    assignment["id"], assignment["lease_owner"], assignment["epoch"]
                )
            elif owner_project is None and project_id and assignment["role"] != "wp":
                if start_key not in self._starting_members:
                    if not self._claim_project_engine(run, project_id):
                        continue
                    self._starting_members.add(start_key)
                    # The legacy API starts a whole project, not one Member.
                    # Multiple durable seats therefore share one in-flight
                    # project bootstrap request.
                    if project_id not in self._starting_projects:
                        self._starting_projects.add(project_id)
                        try:
                            orchestrator.start_project_async(project_id)
                        except Exception as exc:
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

    def _schedule_projects(self, run: dict) -> None:
        orchestrator = self.state.orchestrator
        if orchestrator is None:
            return
        platform = self._platform_for_run(run)
        rows = self.store.challenges(run["id"])
        active = self.store.assignments(run["id"], active_only=True)
        members_by_challenge: dict[str, list[str]] = {}
        for assignment in active:
            if assignment["role"] != "wp":
                members_by_challenge.setdefault(assignment["challenge_id"], []).append(
                    assignment["member"]
                )
        challenges = []
        for row in rows:
            metadata = row["metadata"]
            ready_at = _timestamp(metadata.get("ready_at")) or datetime.now(timezone.utc)
            challenges.append(Challenge(
                row["id"], metadata.get("category", "misc"), ready_at,
                remote=bool(metadata.get("remote")),
                instance_ready=self._instance_ready(row["id"]),
                first_assigned_at=_timestamp(row.get("first_assigned_at")),
                deadline_at=_timestamp(row.get("deadline_at")),
                members=tuple(members_by_challenge.get(row["id"], [])),
                state=row["state"],
            ))
        remote_limit = int(run["config_snapshot"]["workflow_spec"].get("remote_instance_limit", 0))
        with self.state.db.connect() as connection:
            used = connection.execute(
                """SELECT count(*) AS count FROM competition_instances
                   WHERE run_id=%s AND state NOT IN ('stopped','expired')""",
                (run["id"],),
            ).fetchone()["count"]
        plans = plan_assignments(
            challenges,
            occupied_members={item["member"] for item in active},
            remote_capacity=max(0, remote_limit - int(used)),
            now=datetime.now(timezone.utc),
            local_capacity=self.state.config.limits.max_concurrent_tasks,
            available_members={member.name for member in self.state.config.available_members()},
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
                # Reserve the durable competition seat before the asynchronous
                # project bootstrap starts.  This makes the assignment visible
                # to restarts and lets the legacy orchestrator select the same
                # Member instead of racing Diamond's in-memory allocator.
                if self.store.find_assignment(run["id"], challenge["id"], plan.member) is None:
                    try:
                        self.store.assign(
                            run["id"], challenge["id"], plan.member, self.owner,
                            role="primary",
                        )
                    except CompetitionConflict:
                        continue
            elif plan.role == "helper":
                # A helper is a second durable seat on an already-running
                # challenge.  Persist it before asking the orchestrator to
                # reconcile the project so a restart cannot lose the handoff.
                if self.store.find_assignment(run["id"], challenge["id"], plan.member) is None:
                    try:
                        self.store.assign(
                            run["id"], challenge["id"], plan.member, self.owner,
                            role="helper",
                        )
                    except CompetitionConflict:
                        continue
                try:
                    self._starting_members.add(start_key)
                    if project_id not in self._starting_projects:
                        self._starting_projects.add(project_id)
                        orchestrator.start_project_async(project_id)
                except Exception as exc:
                    self._starting_members = {
                        key for key in self._starting_members if key[0] != project_id
                    }
                    self._starting_projects.discard(project_id)
                    self.store.append_run_event(
                        run["id"], "assignment.helper_start_failed",
                        {"challenge_id": challenge["id"], "member": plan.member, "error": str(exc)},
                    )
                continue
            self._starting_members.add(start_key)
            self._starting_projects.add(project_id)
            try:
                orchestrator.start_project_async(project_id)
            except Exception as exc:
                self._starting_members = {
                    key for key in self._starting_members if key[0] != project_id
                }
                self._starting_projects.discard(project_id)
                self.store.append_run_event(
                    run["id"], "assignment.start_failed",
                    {"challenge_id": challenge["id"], "error": str(exc)},
                )

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
                        # Renew halfway through the advertised lease, with a
                        # small floor so a short TTL still gets a chance.
                        return now + timedelta(
                            seconds=max(5, min(3600, remaining // 2))
                        )
        return now + timedelta(seconds=default_seconds)

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
            try:
                renewed = platform.renew_instance(instance["external_id"])
            except Exception as exc:
                rebuilt = getattr(platform, "rebuild_instance", None)
                if callable(rebuilt):
                    try:
                        replacement = rebuilt(instance["external_id"])
                    except Exception as rebuild_exc:
                        replacement = None
                        self.store.append_run_event(
                            run["id"], "instance.rebuild_failed",
                            {
                                "challenge_id": instance["challenge_id"],
                                "error": str(rebuild_exc),
                            },
                        )
                    if replacement is not None:
                        if not isinstance(replacement, dict):
                            replacement = {"state": "ready"}
                        with self.state.db.connect() as connection:
                            connection.execute(
                                """UPDATE competition_instances
                                   SET state='ready',metadata=%s,next_renew_at=%s,updated_at=now()
                                   WHERE challenge_id=%s""",
                                (
                                    Jsonb(replacement),
                                    self._instance_next_renewal(replacement),
                                    instance["challenge_id"],
                                ),
                            )
                            connection.execute(
                                """UPDATE competition_challenges
                                   SET instance_generation=instance_generation+1
                                   WHERE id=%s""",
                                (instance["challenge_id"],),
                            )
                        self.store.append_run_event(
                            run["id"], "instance.rebuilt",
                            {
                                "challenge_id": instance["challenge_id"],
                                "external_id": instance["external_id"],
                            },
                        )
                        logger = getattr(self.state, "logger", None)
                        log_project = getattr(logger, "project", None)
                        if callable(log_project):
                            # Keep the durable run event authoritative while
                            # exposing the lifecycle transition in the same
                            # project stream used by the legacy runtime.
                            log_project(
                                "instance.rebuilt",
                                str(instance["challenge_id"]),
                                run_id=run["id"],
                                external_id=instance["external_id"],
                            )
                        continue
                with self.state.db.connect() as connection:
                    connection.execute(
                        """UPDATE competition_instances SET next_renew_at=now()+interval '60 seconds',
                           updated_at=now() WHERE challenge_id=%s""",
                        (instance["challenge_id"],),
                    )
                self.store.append_run_event(
                    run["id"], "instance.renew_failed",
                    {"challenge_id": instance["challenge_id"], "error": str(exc)},
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

    def _start_instance(self, run: dict, challenge: dict) -> bool:
        platform = self._platform_for_run(run)
        if not self._supports_instance_lifecycle(platform):
            return False
        try:
            result = platform.start_instance(challenge["external_id"])
        except Exception as exc:
            self.store.append_run_event(
                run["id"], "instance.start_failed",
                {"challenge_id": challenge["id"], "error": str(exc)},
            )
            return False
        if not isinstance(result, dict):
            result = {"state": "ready"}
        state = str(result.get("state", "ready")).strip().lower()
        if state not in {"ready", "running", "active"}:
            self.store.append_run_event(
                run["id"], "instance.start_pending",
                {"challenge_id": challenge["id"], "state": state or "unknown"},
            )
            return False
        result.setdefault("state", "ready")
        next_renewal = self._instance_next_renewal(result)
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
        return True

    def _process_submissions(self, run: dict, *, allow_new: bool = True) -> None:
        platform = self._platform_for_run(run)
        self._ingest_project_candidates(run)
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
                continue
            finished = self.store.finish_submission(submission["id"], result)
            if finished["status"] == "correct":
                challenge = next(
                    item for item in self.store.challenges(run["id"])
                    if item["id"] == submission["challenge_id"]
                )
                self._on_correct(run, challenge, submission["candidate"])
        # A paused/draining run may finish an already accepted submission, but
        # must not send a newly queued candidate.  The distinction is kept in
        # the coordinator rather than relying only on the SQL status guard so
        # a future store implementation cannot accidentally submit during a
        # pause.
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
                result = {"verdict": "unknown"}
            finished = self.store.finish_submission(submission["id"], result)
            self.store.append_run_event(
                run["id"], "submission.verdict",
                {"challenge_id": challenge["id"], "submission_id": submission["id"],
                 "verdict": finished["status"]},
            )
            if finished["status"] == "correct":
                self._on_correct(run, challenge, submission["candidate"])
                self._mark_project_candidate(
                    challenge, submission["candidate"], "verified"
                )
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
        project_id = challenge.get("project_id")
        orchestrator = self.state.orchestrator
        if project_id:
            if orchestrator is not None:
                with suppress(Exception):
                    with self.state.db.connect() as connection:
                        accept_verified_flag(
                            connection, project_id, flag,
                            source=f"competition:{run['workflow_id']}",
                        )
                    orchestrator.stop_project(project_id)
            self._release_project_engine(project_id)
        for assignment in self.store.assignments(run["id"], active_only=True):
            if assignment["challenge_id"] == challenge["id"] and assignment["role"] != "wp":
                self.store.release(
                    assignment["id"], assignment["lease_owner"], assignment["epoch"]
                )
        if self._instance_ready(challenge["id"]):
            platform = self._platform_for_run(run)
            with suppress(Exception):
                platform.stop_instance(challenge["external_id"])
            with self.state.db.connect() as connection:
                connection.execute(
                    """UPDATE competition_instances SET state='stopped',updated_at=now()
                       WHERE challenge_id=%s""",
                    (challenge["id"],),
                )

    def _dispatch_wp(self, run: dict) -> None:
        finished = [key for key, future in self._wp_futures.items() if future.done()]
        for key in finished:
            self._wp_futures.pop(key, None)
        while len(self._wp_futures) < 2:
            claimed = self.store.claim_wp_job(run["id"], self.owner)
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
            with suppress(CompetitionConflict):
                self.store.finish_wp_job(assignment, error=str(exc))
            self.store.append_run_event(
                job["run_id"], "wp.deferred",
                {"challenge_id": job["challenge_id"], "member": job["member"],
                 "error": str(exc)},
            )
