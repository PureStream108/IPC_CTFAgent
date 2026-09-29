"""Bounded real ISCTF2025/GZCTF run through the IPC platform tools.

The script reads credentials from the process environment and model keys from
the local test key file. It never prints secrets or candidate flag values.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.core.orchestrator import Orchestrator
from backend.core.state import AppState
from backend.competition.service import CompetitionService
from backend.mcp.mcp_client import MCPClient
from backend.ops.ipc_mcp import build_ipc_mcp
from backend.ops.questions import QuestionStore
from backend.ops.service import OpsAgentService
from backend.ops.store import OpsStore


BASE_URL = "https://gz.imxbt.cn"
DOWNLOAD_URL = "https://download.imxbt.cn"
GAME_ID = 32
TEAM_ID = "Pure_X1cIPC"
KEY_FILE = Path(__file__).resolve().parents[1] / "testconfigkey.txt"
RUN_HOURS = 3
CHALLENGE_SELECT = ["1129", "1130", "1142", "1197", "1208"]


def _test_config() -> dict[str, Any]:
    """Parse the role/model/key table in testconfigkey.txt.

    Expected shape (Chinese punctuation tolerated):
        Model：SpaceBunny：oc_sk_...（用于Member）space-bunny-free
        Model：DeepSeek：oc_sk_...（用于Diamond和IPC）deepseek-v4.1-flash
        baseURL：https://opencode.ai/zen/go/v1/
        思考等级：high
    """

    text = KEY_FILE.read_text(encoding="utf-8-sig")
    base_url = ""
    effort = "high"
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        base_match = re.search(r"baseURL\s*[：:]\s*(\S+)", line, re.IGNORECASE)
        if base_match:
            base_url = base_match.group(1).strip().rstrip("/")
            continue
        effort_match = re.search(r"思考(?:等级|强度)\s*[：:]\s*([A-Za-z]+)", line)
        if effort_match:
            effort = effort_match.group(1).strip().lower()
            continue
        key_match = re.search(r"(oc_sk_|sk-)[A-Za-z0-9_\-]+", line)
        if not key_match:
            continue
        model_match = re.search(r"[）)]\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*$", line)
        scope = " ".join(re.findall(r"[（(]([^）)]*)[）)]", line)).lower()
        entries.append(
            {
                "key": key_match.group(0),
                "model": model_match.group(1) if model_match else "",
                "member": "member" in scope,
                "diamond": "diamond" in scope or "ipc" in scope,
            }
        )
    member = next((item for item in entries if item["member"]), None)
    diamond = next((item for item in entries if item["diamond"]), None)
    if member is None or diamond is None or not member["model"] or not diamond["model"]:
        raise RuntimeError("testconfigkey.txt is missing the Member or Diamond/IPC model entry")
    if not base_url:
        raise RuntimeError("testconfigkey.txt is missing the model base URL")
    return {"base_url": base_url, "effort": effort, "member": member, "diamond": diamond}


def _write_config(directory: Path) -> dict[str, Any]:
    config = _test_config()
    directory.mkdir(parents=True, exist_ok=True)
    members = "\n".join(
        f"  - name: {name}\n"
        "    api_format: openai\n"
        f"    api_key: {config['member']['key']}\n"
        f"    base_url: {config['base_url']}\n"
        f"    model: {config['member']['model']}\n"
        "    api_surface: chat_completions\n"
        f"    reasoning_effort: {config['effort']}"
        for name in (
            "amber", "agate", "topaz", "sugilite", "aventurine",
            "pearl", "sapphire", "jade", "obsidian", "opal",
        )
    )
    (directory / "config.yaml").write_text(
        "log_enabled: true\n"
        "diamond:\n"
        "  api_format: openai\n"
        f"  api_key: {config['diamond']['key']}\n"
        f"  base_url: {config['base_url']}\n"
        f"  model: {config['diamond']['model']}\n"
        "  api_surface: chat_completions\n"
        f"  reasoning_effort: {config['effort']}\n"
        "member: null\n"
        "members:\n"
        + members
        + "\n"
        "runtime:\n"
        "  eval_interval_steps: 20\n"
        "  interval: 2\n"
        "  intent_timeout: 120\n"
        "  reason_timeout: 120\n"
        "  max_members_per_report: 2\n"
        "  sandbox_backend: docker\n"
        "  max_member_steps: 60\n"
        "  max_member_actions_per_task: 20\n"
        "limits:\n"
        "  total_cpu: 12\n"
        "  max_concurrent_tasks: 5\n"
        "  network: true\n",
        encoding="utf-8",
    )
    return config


def _workflow_data() -> dict:
    ends_at = (datetime.now(timezone.utc) + timedelta(hours=RUN_HOURS)).isoformat()
    return {
        "name": "ISCTF2025 game 32 real IPC five challenge run",
        "competition_id": str(GAME_ID),
        "team_id": TEAM_ID,
        "ends_at": ends_at,
        "remote_instance_limit": 0,
        "challenges": {
            "platform": "gzctf",
            "game_id": GAME_ID,
            "list_url": f"{BASE_URL}/api/Game/{GAME_ID}/Challenges",
            "max_challenges": 10,
            "attachment_base_url": DOWNLOAD_URL,
        },
    }


def _safe_progress(state: AppState, run_id: str) -> list[dict[str, object]]:
    with state.db.connect() as connection:
        rows = connection.execute(
            """SELECT c.external_id, c.state,
                      COALESCE(array_agg(DISTINCT s.status) FILTER (WHERE s.status IS NOT NULL), '{}') AS submissions
                 FROM competition_challenges c
                 JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                 LEFT JOIN competition_submissions s ON s.challenge_id=c.id
                WHERE rc.run_id=%s
                GROUP BY c.external_id,c.state
                ORDER BY c.external_id""",
            (run_id,),
        ).fetchall()
    return [
        {
            "external_id": str(row["external_id"]),
            "state": str(row["state"]),
            "submission_statuses": [str(item) for item in (row["submissions"] or [])],
        }
        for row in rows
    ]


def _assignment_stats(state: AppState, run_id: str) -> dict[str, int]:
    with state.db.connect() as connection:
        row = connection.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE released_at IS NOT NULL) AS released,
                      count(*) FILTER (WHERE released_at IS NULL) AS active
                 FROM competition_assignments WHERE run_id=%s""",
            (run_id,),
        ).fetchone()
    return {
        "assignment_total": int(row["total"]),
        "assignment_released": int(row["released"]),
        "assignment_active": int(row["active"]),
    }


def _session_stats(state: AppState, run_id: str) -> dict[str, int]:
    """Prove Members really work: durable session events per member seat."""
    with state.db.connect() as connection:
        row = connection.execute(
            """SELECT count(*) AS events,
                      count(DISTINCT s.member) AS members_with_events
                 FROM agent_events e
                 JOIN competition_sessions s ON s.id=e.session_id
                WHERE s.run_id=%s""",
            (run_id,),
        ).fetchone()
    return {
        "session_events": int(row["events"]),
        "members_with_events": int(row["members_with_events"]),
    }


async def _run() -> dict[str, object]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = Path(__file__).resolve().parents[1] / ".qa-artifacts" / f"isctf-real-{stamp}"
    config_dir = root / "config"
    artifact_root = root / "artifacts"
    model_config = _write_config(config_dir)
    os.environ["IPC_ARTIFACT_ROOT"] = str(artifact_root)
    os.environ["IPC_INSTANCE_ID"] = f"isctf-real-{uuid.uuid4().hex[:8]}"

    state = AppState(root=root, config_dir=config_dir)
    orchestrator = Orchestrator(state)
    state.orchestrator = orchestrator
    orchestrator.start()
    ops = OpsAgentService(state)
    ops.update_config(
        api_format="openai",
        api_surface="chat_completions",
        reasoning_effort=model_config["effort"],
        api_key=model_config["diamond"]["key"],
        base_url=model_config["base_url"],
        model=model_config["diamond"]["model"],
    )
    state.ops_agent_service = ops
    competition = CompetitionService(state, sync_interval=30, tick_interval=0.5, ops_service=ops)
    state.competition = competition
    competition.start_worker()

    workflow = ops.create_workflow(_workflow_data())
    workflow_id = str(workflow["id"])
    session = OpsStore(state.root, database=state.db).create_session("ISCTF2025 real platform run")
    server = build_ipc_mcp(lambda: state)
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-ipc-ops-session": session["id"]})
        )
    )

    async def call_question(name: str, arguments: dict[str, object]):
        return await server._tool_manager.call_tool(name, arguments, context=context)  # type: ignore[attr-defined]

    try:
        credentials = [
            ("username", "gzctf_username", os.environ.get("IPC_GZ_USERNAME", ""), True),
            ("password", "gzctf_password", os.environ.get("IPC_GZ_PASSWORD", ""), True),
            ("token", "gzctf_token", os.environ.get("IPC_GZ_TOKEN", ""), False),
        ]
        for suffix, secret_name, value, required in credentials:
            if not value:
                if required:
                    raise RuntimeError(f"missing process credential: {secret_name}")
                continue
            question = await call_question(
                "ipc_question",
                {
                    "operation_key": f"isctf2025-real-{suffix}",
                    "title": f"Provide the ISCTF2025 GZCTF participant {suffix}.",
                    "options": ["Use the configured participant credential"],
                    "workflow_id": workflow_id,
                    "secret_name": secret_name,
                },
            )
            QuestionStore(state).answer(question["id"], session["id"], value)
            answer = await call_question(
                "ipc_question_result", {"question_id": question["id"]}
            )
            if answer.get("state") != "answered":
                raise RuntimeError(f"IPC credential question was not answered: {secret_name}")

        confirmation = ops.confirm_workflow(workflow_id, f"CONFIRM WORKFLOW {workflow_id}")
        del confirmation

        async with MCPClient.in_process(server) as client:
            preflight = await client.call_tool("ipc_platform_preflight", {"workflow_id": workflow_id})
            if not preflight.get("ok"):
                raise RuntimeError(json.dumps({"phase": "preflight", "error": preflight.get("error")}))
            start = await client.call_tool(
                "ipc_start_platform",
                {
                    "workflow_id": workflow_id,
                    "idempotency_key": f"isctf2025-real-{uuid.uuid4().hex}",
                    "select": CHALLENGE_SELECT,
                },
            )
            if not start.get("ok"):
                raise RuntimeError(json.dumps({"phase": "start", "error": start.get("error")}))
            run_id = str(start["run"]["id"])
            print(json.dumps({"workflow_id": workflow_id, "run_id": run_id, "challenge_count": preflight.get("challenge_count"), "allocator": start.get("allocator")}, ensure_ascii=False), flush=True)

            target_count = len(_safe_progress(state, run_id))
            deadline = time.monotonic() + RUN_HOURS * 3600 + 10 * 60
            started_at = time.monotonic()
            last_print = 0.0
            progress: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                status = await client.call_tool("ipc_platform_status", {"run_id": run_id})
                progress = _safe_progress(state, run_id)
                run_status = (status.get("run") or {}).get("status")
                now = time.monotonic()
                if now - last_print >= 30:
                    print(json.dumps({"elapsed_seconds": int(now - started_at), "run_status": run_status, "members": sum(item.get("state") in {"primary", "helper", "wp"} for item in status.get("members", [])), **_session_stats(state, run_id), "progress": progress}, ensure_ascii=False), flush=True)
                    last_print = now
                if run_status in {"finished", "stopped", "cancelled"}:
                    break
                if len(progress) == target_count and all(item["state"] in {"solved", "expired", "blocked", "withdrawn", "cancelled"} for item in progress):
                    break

            final = await client.call_tool("ipc_platform_status", {"run_id": run_id})
            progress = _safe_progress(state, run_id)
            return {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "run_status": (final.get("run") or {}).get("status"),
                "members_assigned": sum(item.get("state") in {"primary", "helper", "wp"} for item in final.get("members", [])),
                **_assignment_stats(state, run_id),
                **_session_stats(state, run_id),
                "progress": progress,
                "artifact_root": str(artifact_root),
            }
    finally:
        try:
            active = competition.store.active_run()
            if active is not None and active["workflow_id"] == workflow_id:
                try:
                    competition.control(active["id"], "stop", int(active["revision"]))
                except Exception:
                    pass
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
