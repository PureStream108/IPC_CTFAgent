"""Test-only entrypoint: production orchestration with scripted mock Members."""

from contextlib import asynccontextmanager

from backend.core.config import MEMBER_NAMES
from backend.core.orchestrator import Orchestrator
from backend.server.app import create_app

SCRIPT = [
    {"action": "bash", "command": "sleep 2; printf 'e2e-sandbox-command-ok\\n'"},
    {"action": "tool", "server": "memory", "tool": "memory_catalog", "args": {}},
    {"action": "flag", "flag": "flag{e2e-deterministic}",
     "description": "Reproduced sandbox command and memory MCP evidence in the acceptance fixture."},
]


class FixtureScripts(dict):
    """Choose the next deterministic turn from durable platform progress."""
    def __init__(self, state):
        super().__init__({name: SCRIPT for name in MEMBER_NAMES})
        self.state = state

    def get(self, key, default=None):
        if isinstance(key, tuple):
            with self.state.db.connect() as conn:
                row = conn.execute(
                    "SELECT p.platform,c.metadata FROM projects p JOIN competition_challenges c ON c.project_id=p.id WHERE p.id=%s",
                    (key[0],),
                ).fetchone()
            if row and row["platform"] == "tsecbench":
                count = (row["metadata"].get("flag_progress") or {}).get("correct_flag_count", 0)
                flag = "flag{e2e-tsec-two}" if count else "flag{e2e-tsec-one}"
                return [*SCRIPT[:-1], {"action": "flag", "flag": flag, "description": "Verified the next deterministic benchmark objective."}]
        return super().get(key, default)

app = create_app()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        state = application.state.ipc
        state.orchestrator.shutdown()
        state.orchestrator = Orchestrator(
            state, scripts=FixtureScripts(state)
        )
        state.orchestrator.start()
        yield


app.router.lifespan_context = lifespan


@app.get("/e2e/runs/{run_id}/evidence")
def evidence(run_id: str):
    """Read durable evidence. No production API or solver behavior is replaced."""
    state = app.state.ipc
    store = state.competition.store
    assignments = store.assignments(run_id)
    events = [event for session in {a["session_id"] for a in assignments}
              for event in store.events(session, limit=500)]
    tool_names = [e["payload"].get("name") for e in events if e["kind"] == "tool_call"]
    with state.db.connect() as connection:
        jobs = connection.execute(
            "SELECT status,attempts,artifact_path FROM competition_wp_jobs WHERE run_id=%s",
            (run_id,),
        ).fetchall()
        backlog = connection.execute(
            """SELECT count(*) AS count FROM competition_messages m
               LEFT JOIN competition_message_cursors c
                 ON c.session_id=m.envelope->>'receiver_session_id'
               WHERE m.run_id=%s AND m.sequence>COALESCE(c.sequence,0)""",
            (run_id,),
        ).fetchone()["count"]
    return {
        "assignments": assignments,
        "tool_names": tool_names,
        "tool_results": [e["payload"] for e in events if e["kind"] == "tool_result"],
        "wp_jobs": jobs,
        "collaboration_backlog": int(backlog),
        "run_events": store.run_events(run_id, limit=1000),
    }
