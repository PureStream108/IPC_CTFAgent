"""Resume a paused ISCTF2025/GZCTF run with its original Members and sessions.

The paused run keeps its durable challenges, sessions, submissions and seat
history.  This script resumes that run, monitors it until the declared end or
completion, and pauses again on exit so the same sessions can resume later.
It never prints secrets or candidate flag values.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from isctf_real_ipc_run import (  # noqa: E402
    RUN_HOURS,
    _assignment_stats,
    _safe_progress,
    _session_stats,
    _write_config,
)

from backend.core.orchestrator import Orchestrator  # noqa: E402
from backend.core.state import AppState  # noqa: E402
from backend.competition.service import CompetitionService, _timestamp  # noqa: E402
from backend.ops.service import OpsAgentService  # noqa: E402

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else None


async def _run() -> dict[str, object]:
    ops_source = os.environ.get("IPC_RESUME_OPS_SOURCE", "").strip()
    if ops_source:
        root = Path(ops_source)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        root = Path(__file__).resolve().parents[1] / ".qa-artifacts" / f"isctf-resume-{stamp}"
    config_dir = root / "config"
    artifact_root = root / "artifacts"
    _write_config(config_dir)
    os.environ["IPC_ARTIFACT_ROOT"] = str(artifact_root)
    os.environ["IPC_INSTANCE_ID"] = f"isctf-resume-{uuid.uuid4().hex[:8]}"

    state = AppState(root=root, config_dir=config_dir)
    orchestrator = Orchestrator(state)
    state.orchestrator = orchestrator
    orchestrator.start()
    ops = OpsAgentService(state)
    state.ops_agent_service = ops
    competition = CompetitionService(state, sync_interval=30, tick_interval=0.5, ops_service=ops)
    state.competition = competition

    run_row = competition.store.get_run(RUN_ID) if RUN_ID else competition.store.active_run()
    if run_row is None:
        raise RuntimeError("no resumable run found (nothing in a non-terminal state)")
    run_id = str(run_row["id"])
    if run_row["status"] != "paused":
        raise RuntimeError(f"run {run_id} is {run_row['status']}, not paused; refusing to resume")
    resumed = competition.control(run_id, "resume", int(run_row["revision"]))
    print(json.dumps({"run_id": run_id, "run_status": resumed["run"]["status"], "resumed": True}, ensure_ascii=False), flush=True)
    competition.start_worker()

    ends_at = _timestamp(
        run_row["config_snapshot"]["workflow_spec"].get("ends_at")
    )
    if ends_at is None:
        ends_at = datetime.now(timezone.utc) + timedelta(hours=RUN_HOURS)
    try:
        deadline = time.monotonic() + max(60.0, (ends_at - datetime.now(timezone.utc)).total_seconds()) + 10 * 60
        started_at = time.monotonic()
        last_print = 0.0
        progress: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            progress = _safe_progress(state, run_id)
            run_status = str(competition.store.get_run(run_id)["status"])
            now = time.monotonic()
            if now - last_print >= 30:
                print(json.dumps({"elapsed_seconds": int(now - started_at), "run_status": run_status, **_session_stats(state, run_id), "progress": progress}, ensure_ascii=False), flush=True)
                last_print = now
            if run_status in {"finished", "stopped"}:
                break
            if progress and all(item["state"] in {"solved", "expired", "blocked", "withdrawn", "cancelled"} for item in progress):
                break

        final_status = str(competition.store.get_run(run_id)["status"])
        return {
            "run_id": run_id,
            "run_status": final_status,
            **_assignment_stats(state, run_id),
            **_session_stats(state, run_id),
            "progress": _safe_progress(state, run_id),
            "artifact_root": str(artifact_root),
        }
    finally:
        try:
            current = competition.store.get_run(run_id)
            if current["status"] in {"running", "blocked"}:
                competition.control(run_id, "pause", int(current["revision"]))
                print(json.dumps({"run_id": run_id, "paused": True}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"pause_on_exit_error": str(exc)}, ensure_ascii=False), flush=True)
        finally:
            competition.shutdown()
            orchestrator.shutdown()
            state.close()
            shutil.rmtree(config_dir, ignore_errors=True)


def main() -> int:
    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
