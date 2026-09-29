"""Run a bounded ISCTF2025 discovery and IPC allocation smoke test.

The target is deliberately fixed to the public scoreboard endpoint for game
32.  The smoke test never submits a flag and never follows links to another
competition.  It uses IPC's MCP tools for project allocation and question
handling so the same path is exercised as the runner integration.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from psycopg.types.json import Jsonb

from backend.blackboard import graph_store
from backend.competition.fixture import FixturePlatform
from backend.competition.service import CompetitionService
from backend.ops.models import PlatformWorkflowSpec
from backend.mcp.mcp_client import MCPClient
from backend.ops.ipc_mcp import build_ipc_mcp
from backend.ops.questions import QuestionStore
from backend.ops.store import OpsStore
from backend.platform.gzctf import GZCTFAdapter, GZCTFClient
from backend.platform.mapping import FieldMapping
from backend.server.app import create_app


BASE_URL = "https://gz.imxbt.cn"
GAME_ID = 32
SCOREBOARD_URL = f"{BASE_URL}/api/Game/{GAME_ID}/Scoreboard"
MAX_CHALLENGES = 10
KEY_FILE = Path(__file__).resolve().parents[1] / "testconfigkey.txt"


def write_mock_config(config_dir: Path) -> Path:
    """Create a local, no-network Member configuration for the smoke run."""

    config_dir.mkdir(parents=True, exist_ok=True)
    member_names = (
        "amber", "agate", "topaz", "sugilite", "aventurine",
        "pearl", "sapphire", "jade", "obsidian", "opal",
    )
    members = "".join(
        "- name: " + name + "\n"
        "  api_format: mock\n"
        "  api_key: smoke-key\n"
        "  base_url: https://mock.invalid\n"
        "  model: mock\n"
        for name in member_names
    )
    (config_dir / "config.yaml").write_text(
        "log_enabled: true\n"
        "diamond:\n"
        "  api_format: mock\n"
        "  api_key: ''\n"
        "  base_url: ''\n"
        "  model: mock\n"
        "members:\n"
        + members
        + "runtime:\n"
        "  eval_interval_steps: 20\n"
        "  interval: 2\n"
        "  intent_timeout: 30\n"
        "  reason_timeout: 30\n"
        "  max_members_per_report: 4\n"
        "  sandbox_backend: local\n"
        "  max_member_steps: 1\n"
        "  max_member_actions_per_task: 1\n",
        encoding="utf-8",
    )
    source = Path(__file__).resolve().parents[1] / "backend" / "config"
    for name in ("models.yaml", "limits.yaml"):
        shutil.copy(source / name, config_dir / name)
    return config_dir


def _assert_target(url: str) -> None:
    if url != SCOREBOARD_URL:
        raise ValueError("smoke test is restricted to the ISCTF2025 scoreboard")


def fetch_challenges(limit: int) -> list[dict[str, Any]]:
    if not 1 <= limit <= MAX_CHALLENGES:
        raise ValueError(f"limit must be between 1 and {MAX_CHALLENGES}")
    _assert_target(SCOREBOARD_URL)
    mapping = FieldMapping(platform="gzctf", game_id=GAME_ID)
    with GZCTFClient(base_url=BASE_URL) as client:
        adapter = GZCTFAdapter(client, mapping)
        normalized = adapter.fetch_public_scoreboard(limit=limit)
    return [
        {
            "external_id": item.external_id,
            "title": item.title,
            "category": item.category,
            "score": item.platform_data.get("score"),
        }
        for item in normalized
    ]


def _load_key_metadata() -> dict[str, Any]:
    """Validate the supplied key file without returning secret values."""

    lines = KEY_FILE.read_text(encoding="utf-8-sig").splitlines()
    keys = [line.strip() for line in lines[:3] if line.strip().startswith("sk-")]
    if len(keys) != 3 or any(len(key) < 16 for key in keys):
        raise ValueError("testconfigkey.txt must contain three model keys")
    return {
        "member_key_set": bool(keys[0]),
        "diamond_key_set": bool(keys[1]),
        "ipc_key_set": bool(keys[2]),
        "base_url": "https://api.kimi.com/coding/v1",
        "reasoning_effort": "low",
        "model": "K3",
    }


async def _call_tools(app, challenges: list[dict[str, Any]]) -> dict[str, Any]:
    state = app.state.ipc
    spec = PlatformWorkflowSpec.model_validate(
        {
            "name": "ISCTF2025 bounded smoke",
            "competition_id": str(GAME_ID),
            "team_id": "public-scoreboard-smoke",
            "remote_instance_limit": 0,
            "challenges": {
                "platform": "gzctf",
                "game_id": GAME_ID,
                "list_url": SCOREBOARD_URL,
            },
            "submit": {
                "url": f"{BASE_URL}/api/Game/{GAME_ID}/Challenges/{{{{external_id}}}}",
                "json_template": {"flag": "{{flag}}"},
                "success_statuses": [200],
                "success_values": [True],
                "wrong_values": [False],
            },
        }
    )
    workflow_id = f"wf_isctf2025_smoke_{uuid.uuid4().hex[:8]}"
    digest = hashlib.sha256(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    workflow = {
        "id": workflow_id,
        "status": "confirmed",
        "confirmed_digest": digest,
        "spec_digest": digest,
        "spec": spec,
    }

    class WorkflowStore:
        def get_workflow(self, requested: str) -> dict[str, Any]:
            if requested != workflow_id:
                raise KeyError(requested)
            return workflow

    class FixtureOps:
        def __init__(self) -> None:
            self.db = state.db
            self.store = WorkflowStore()
            self.projects: dict[str, str] = {}

        def _import(self, _workflow_id: str, _spec: PlatformWorkflowSpec, select: list[str]):
            imported = []
            with self.db.connect() as connection:
                for external_id in select:
                    project_id = self.projects.get(external_id)
                    if project_id is None:
                        project_id = graph_store.create_project(
                            connection,
                            next(item["title"] for item in challenges if item["external_id"] == external_id),
                            SCOREBOARD_URL,
                            "Allocate IPC Members for smoke testing.",
                            next(item["category"] for item in challenges if item["external_id"] == external_id),
                            external_id=external_id,
                            platform="fixture",
                        )
                        connection.execute(
                            "INSERT INTO workflow_challenges (workflow_id,external_id,project_id) VALUES (%s,%s,%s)",
                            (workflow_id, external_id, project_id),
                        )
                        self.projects[external_id] = project_id
                    imported.append({"external_id": external_id, "project_id": project_id, "created": True})
            return {"operation": "import", "imported": imported}

    class Allocator:
        def __init__(self) -> None:
            self.started: list[str] = []

        def start_project_async(self, project_id: str) -> dict[str, str]:
            self.started.append(project_id)
            return {"project_id": project_id, "status": "queued"}

        def active_member_owners(self) -> dict[str, str]:
            return {}

        def stop_project(self, _project_id: str) -> None:
            return None

        def shutdown(self) -> None:
            return None

    platform = FixturePlatform(
        [
            __import__("backend.platform.mapping", fromlist=["PlatformChallenge"]).PlatformChallenge(
                external_id=item["external_id"],
                title=item["title"],
                category=item["category"] if item["category"] in {"pwn", "reverse", "crypto", "web", "misc", "ai", "osint"} else "misc",
                description="public ISCTF2025 scoreboard entry",
            )
            for item in challenges
        ]
    )
    with state.db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,status,confirmed_digest,created_at,updated_at)
               VALUES (%s,'smoke',%s,%s,%s,'confirmed',%s,now(),now())""",
            (workflow_id, spec.name, Jsonb(spec.model_dump(mode="json")), digest, digest),
        )
    old_competition = getattr(state, "competition", None)
    old_orchestrator = getattr(state, "orchestrator", None)
    if old_competition is not None:
        old_competition.shutdown()
    if old_orchestrator is not None:
        old_orchestrator.shutdown()
    state.orchestrator = Allocator()
    state.ops_agent_service = FixtureOps()
    state.competition = CompetitionService(
        state,
        tick_interval=0.01,
        platform_factory=lambda _workflow, _spec: platform,
        ops_service=state.ops_agent_service,
    )
    state.competition.start_worker()
    server = build_ipc_mcp(lambda: state)
    async with MCPClient.in_process(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        required = {
            "ipc_start_platform",
            "ipc_platform_status",
            "ipc_question",
            "ipc_question_result",
        }
        missing = sorted(required - tools)
        if missing:
            raise RuntimeError(f"IPC MCP is missing required tools: {missing}")

        allocation = await client.call_tool(
            "ipc_start_platform",
            {"workflow_id": workflow_id, "idempotency_key": f"isctf2025-smoke-{uuid.uuid4().hex[:12]}"},
        )
        if not allocation.get("ok"):
            raise RuntimeError(json.dumps(allocation, ensure_ascii=False))
        run_id = allocation["run"]["id"]
        for _ in range(3):
            state.competition.tick()
        platform_status = await client.call_tool("ipc_platform_status", {"run_id": run_id})
        for _ in range(40):
            run_members = platform_status.get("members", [])
            assigned = sum(
                item.get("state") in {"primary", "helper", "wp"}
                for item in run_members
            )
            if assigned >= len(challenges):
                break
            state.competition.tick()
            await asyncio.sleep(0.05)
            platform_status = await client.call_tool("ipc_platform_status", {"run_id": run_id})
        if assigned < len(challenges):
            raise RuntimeError(
                f"IPC did not reserve all requested seats: {assigned}/{len(challenges)}"
            )

        session = OpsStore(state.root, database=state.db).create_session("ISCTF2025 smoke")
        context = SimpleNamespace(
            request_context=SimpleNamespace(
                request=SimpleNamespace(
                    headers={"x-ipc-ops-session": session["id"]},
                )
            )
        )

        async def call_session_tool(name: str, arguments: dict[str, Any]) -> Any:
            return await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                name,
                arguments,
                context=context,
            )

        question = await call_session_tool(
            "ipc_question",
            {
                "operation_key": "isctf2025-platform-token",
                "title": "Provide the participant credential for ISCTF2025 game 32.",
                "options": ["GZCTF participant token", "Username and password"],
                "workflow_id": workflow_id,
                "secret_name": "gzctf_token",
            },
        )
        if question.get("state") != "pending":
            raise RuntimeError("IPC credential question was not created as pending")
        QuestionStore(state).answer(
            question["id"],
            session["id"],
            f"ipc-smoke-placeholder-{uuid.uuid4().hex}",
        )
        question_result = await call_session_tool(
            "ipc_question_result",
            {"question_id": question["id"]},
        )
        workflow_secret_names = sorted(
            OpsStore(state.root, database=state.db).workflow_secrets(workflow_id)
        )
        run_view = platform_status["run"]
        if isinstance(run_view.get("run"), dict):
            run_view = run_view["run"]
        return {
            "allocator": allocation.get("allocator"),
            "run_id": run_id,
            "platform_status": {
                "run_status": run_view["status"],
                "assigned_members": sum(
                    item.get("state") in {"primary", "helper", "wp"}
                    for item in platform_status["members"]
                ),
                "assignments": [
                    {
                        "member": item["member"],
                        "role": (item.get("assignment") or {}).get("role", item.get("state")),
                    }
                    for item in platform_status["members"]
                    if item.get("state") in {"primary", "helper", "wp"}
                ],
            },
            "question": {
                "tools_present": sorted(required),
                "question_id": question["id"],
                "state": question_result["state"],
                "answer_type": sorted(question_result["answer"]),
                "workflow_secret_names": workflow_secret_names,
            },
            "mcp_tools": sorted(tools),
        }


def run(limit: int) -> dict[str, Any]:
    challenges = fetch_challenges(limit)
    key_metadata = _load_key_metadata()
    with tempfile.TemporaryDirectory(prefix="ipc-isctf-smoke-") as temporary:
        root = Path(temporary)
        config_dir = write_mock_config(root / "config")
        os.environ["IPC_ROOT"] = str(root)
        os.environ["IPC_ARTIFACT_ROOT"] = str(root / "artifacts")
        os.environ["IPC_ALLOW_LOCAL_SANDBOX"] = "1"
        app = create_app(root=root)
        async def run_app() -> dict[str, Any]:
            async with app.router.lifespan_context(app):
                app.state.ipc.config_dir = config_dir
                app.state.ipc.reload_config()
                try:
                    result = await _call_tools(app, challenges)
                    result["key_metadata"] = key_metadata
                    result["challenge_count"] = len(challenges)
                    return result
                finally:
                    competition = getattr(app.state.ipc, "competition", None)
                    if competition is not None:
                        with suppress(Exception):
                            competition.shutdown()
                        active = competition.store.active_run()
                        if active is not None and str(active["workflow_id"]).startswith(
                            "wf_isctf2025_smoke_"
                        ):
                            with suppress(Exception):
                                competition.control(
                                    active["id"],
                                    "stop",
                                    int(active["revision"]),
                                )
        return asyncio.run(run_app())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    result = run(args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
