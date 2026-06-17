from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from backend.blackboard import edge_store
from backend.blackboard import graph_store
from backend.core.orchestrator import Orchestrator
from backend.core.state import AppState


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value or default


TITLE = _env("IPC_SOLVER_TITLE", "CTF Web 127.0.0.1:8998 Standalone")
ORIGIN = _env("IPC_SOLVER_ORIGIN", "这是一道CTFWEB题目：http://127.0.0.1:8998/，请完成。")
GOAL = _env("IPC_SOLVER_GOAL", "获取FLAG。")
CATEGORY = _env("IPC_SOLVER_CATEGORY", "web")
HINT = _env(
    "IPC_SOLVER_HINT",
    "If you are inside a containerized sandbox, use http://host.docker.internal:8998/ "
    "to reach the target. 127.0.0.1 inside the container points to the container itself.",
)
TIMEOUT = int(os.environ.get("IPC_SOLVER_TIMEOUT", "1800"))


def create_project(state: AppState) -> str:
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, TITLE, ORIGIN, GOAL, CATEGORY, [])
        graph_store.create_hint(conn, pid, HINT, "ipc")
    state.logger.project("standalone_project_created", pid, title=TITLE, category=CATEGORY)
    return pid


def project_snapshot(state: AppState, project_id: str) -> dict | None:
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        return None
    return {
        "status": detail.project.status,
        "flag": detail.project.flag,
        "facts": len(detail.facts),
        "reports": len(detail.reports),
        "open_intents": sum(1 for intent in detail.intents if intent.to is None),
        "latest_fact_id": detail.facts[-1].id if detail.facts else None,
        "latest_fact": detail.facts[-1].description if detail.facts else "",
        "agents": [{ "name": agent.name, "state": agent.state } for agent in detail.agents],
    }


def seed_followup_intent(state: AppState, project_id: str, latest_fact_id: str | None, latest_fact: str) -> str | None:
    if latest_fact_id is None or latest_fact_id == "goal":
        return None
    text = latest_fact.lower()
    if any(keyword in text for keyword in ("sandbox", "exec", "blacklist", "/check", "python")):
        description = (
            "Use the confirmed sandbox constraints to craft a blacklist-safe exploit for /check "
            "and retrieve the real flag."
        )
    else:
        description = (
            "Continue directly from the latest confirmed fact and take the next concrete step "
            "toward retrieving the flag."
        )
    with state.db.connect() as conn:
        existing = edge_store.find_similar_open_intent(conn, project_id, [latest_fact_id], description)
        if existing is not None:
            return existing.id
        intent = edge_store.create_intent(conn, project_id, [latest_fact_id], description, "diamond")
        graph_store.add_link(conn, project_id, "diamond", "aventurine", "assign")
        graph_store.add_link(conn, project_id, "aventurine", f"intent:{intent.id}", "explore")
    state.logger.project(
        "standalone_followup_seeded",
        project_id,
        intent=intent.id,
        from_fact=latest_fact_id,
        description=description,
    )
    return intent.id


def main() -> int:
    root = Path.cwd()
    state = AppState(root=root)
    orch = Orchestrator(state)
    state.orchestrator = orch
    orch.start()
    project_id = create_project(state)
    print(f"PID={project_id}", flush=True)
    orch.start_project(project_id)

    deadline = time.time() + TIMEOUT
    last_snapshot: dict | None = None
    followup_seeds = 0
    consecutive_db_errors = 0
    try:
        while time.time() < deadline:
            try:
                snap = project_snapshot(state, project_id)
                consecutive_db_errors = 0
            except sqlite3.OperationalError as exc:
                consecutive_db_errors += 1
                print(f"DB_RETRY {consecutive_db_errors} {exc}", flush=True)
                if consecutive_db_errors >= 5:
                    raise
                time.sleep(2)
                continue
            if snap is None:
                print("PROJECT_MISSING", flush=True)
                return 1
            if snap != last_snapshot:
                print(json.dumps(snap, ensure_ascii=False), flush=True)
                last_snapshot = snap
            if (
                snap["status"] == "running"
                and snap["open_intents"] == 0
                and snap["flag"] is None
                and followup_seeds < 3
            ):
                seeded = seed_followup_intent(
                    state,
                    project_id,
                    snap["latest_fact_id"],
                    snap["latest_fact"],
                )
                if seeded is not None:
                    followup_seeds += 1
                    print(f"SEEDED_INTENT {seeded}", flush=True)
            if snap["status"] in ("completed", "stopped"):
                break
            time.sleep(3)
        else:
            print("TIMEOUT", flush=True)
            return 124

        final = project_snapshot(state, project_id)
        print("FINAL", json.dumps(final, ensure_ascii=False), flush=True)
        return 0
    finally:
        orch.shutdown()


if __name__ == "__main__":
    sys.exit(main())
