"""P5 docker E2E probe: run inside ipc-app against the compose Postgres.

Proves the deployed image's P5 driver unification deterministically, using only
in-process construction so it never perturbs the live coordinator:

  P5.1 standalone streaming: a scripted mock member on a standalone project is
      driven by the provider-native AgentRuntime - a durable ``project`` agent
      session exists, tool_call/tool_result events landed, the graph side
      effects are identical to the legacy driver's, and no ``member_driver``
      fallback event was logged.
  P5.2 streaming mock: MockAdapter.turn emits exactly one native tool call per
      scripted action, in the assistant ``tool_calls`` shape the runtime reads,
      and goes idle (no invented actions) once the script runs out.
  P5.3 legacy fallback: the MEMBER_LEGACY_DRIVER feature flag routes a scripted
      member back to the legacy loop and logs member_driver=legacy_fallback
      with the reason; a decide-only adapter falls back the same way.

Cleans up the probe projects it created. Prints PASS/FAIL per check and exits
non-zero if any fail.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

# Error boundaries keep their recovery behavior and retain debug diagnostics.
_recovery_logger = logging.getLogger(__name__)

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


PROBE_TAG = "p5probe"


def _member_deps(db, tmp):
    from backend.core.logging_util import IPCLogger
    from backend.mcp.mcp_client import MCPRegistry
    from backend.mcp.shared import build_browser_mcp
    from backend.members.base_member import MemberDeps
    from backend.memory.memory_mcp import build_memory_mcp
    from backend.memory.memory_store import MemoryStore
    from backend.sandbox.sandbox import LocalSandbox
    from backend.tools.tool_registry import ToolRegistry

    os.environ.setdefault("IPC_ALLOW_LOCAL_SANDBOX", "1")
    mem = MemoryStore(db).configure()
    mcps = MCPRegistry()
    mcps.register(build_memory_mcp(mem))
    mcps.register(build_browser_mcp())
    sandbox = LocalSandbox("p5probe", tmp / "ws")
    sandbox.start()
    return MemberDeps(
        db=db,
        logger=IPCLogger(tmp / "logs", enabled=True),
        sandbox=sandbox,
        mcps=mcps,
        registry=ToolRegistry().load(),
        memory=mem,
        eval_interval=7,
        max_steps=20,
    )


def _project(db, suffix):
    from backend.blackboard import edge_store, graph_store

    with db.connect() as conn:
        pid = graph_store.create_project(
            conn, f"{PROBE_TAG}-{suffix}", "origin", "get flag", "web"
        )
        intent = edge_store.create_intent(conn, pid, ["origin"], "explore web app", "diamond")
    return pid, intent.id


SCRIPT = [
    {"action": "bash", "command": "echo recon"},
    {"action": "intent", "from": ["origin"], "description": f"{PROBE_TAG} probe login"},
    {"action": "report", "progress": "found a login form", "difficulty": "medium",
     "steps": ["recon"], "directions": ["try sqli"], "knowledge": ["web"]},
    {"action": "conclude", "description": f"{PROBE_TAG} login form confirmed"},
]


def _snapshot(db, pid):
    from backend.blackboard import edge_store, graph_store, node_store

    with db.connect() as conn:
        facts = {f.id: f.description for f in node_store.list_facts(conn, pid)}
        intents = [
            (
                i.description,
                tuple(facts.get(fid, fid) for fid in (i.from_ or ())),
                facts.get(i.to, i.to),
            )
            for i in edge_store.list_intents(conn, pid)
        ]
        reports = [
            (r.member, r.progress, r.difficulty, tuple(r.steps),
             tuple(r.directions), tuple(r.knowledge))
            for r in graph_store.list_reports(conn, pid)
        ]
    return sorted(facts.values()), sorted(intents), sorted(reports)


def p51_standalone_streaming_and_equivalence(db, tmp):
    from backend.agent.session import AgentSessionStore
    from backend.core.config import MemberConfig
    from backend.members import base_member
    from backend.members.factory import create_member

    pid, iid = _project(db, "stream")
    deps = _member_deps(db, tmp / "stream")
    member = create_member(MemberConfig(name="jade", api_format="mock"), deps, script=SCRIPT)
    result = member.solve(pid, iid, "web", is_initial=False)
    check("P5.1 standalone scripted member concludes via streaming",
          result.status == "concluded", f"status={result.status} steps={result.steps}")

    store = AgentSessionStore(db)
    session = store.active_session_for_task(f"member:{pid}:jade:{iid}")
    events = store.events(session["id"], after=0, limit=500) if session else []
    check("P5.1 durable project session holds the tool transcript",
          session is not None and session["kind"] == "project"
          and any(e["kind"] == "tool_call" for e in events)
          and any(e["kind"] == "tool_result" for e in events),
          f"events={len(events)}")
    entries = deps.logger.read_log("project", pid, None)
    check("P5.1 streaming path never touches the legacy fallback event",
          not any(e["event"] == "member_driver" for e in entries))

    base_member.MEMBER_LEGACY_DRIVER = True
    try:
        pid2, iid2 = _project(db, "legacy")
        deps2 = _member_deps(db, tmp / "legacy")
        legacy = create_member(
            MemberConfig(name="jade", api_format="mock"), deps2, script=SCRIPT
        )
        result2 = legacy.solve(pid2, iid2, "web", is_initial=False)
        check("P5.1 legacy fallback concludes the same task",
              result2.status == "concluded" and result2.steps == result.steps,
              f"status={result2.status} steps={result2.steps}")
        check("P5.1 streaming and legacy graph writes are identical",
              _snapshot(db, pid) == _snapshot(db, pid2))
        entries2 = deps2.logger.read_log("project", pid2, None)
        driver = [e for e in entries2 if e["event"] == "member_driver"]
        check("P5.3 feature-flag fallback is observable",
              len(driver) == 1 and driver[0]["driver"] == "legacy_fallback"
              and driver[0]["reason"] == "feature_flag",
              driver[0]["reason"] if driver else "none")
    finally:
        base_member.MEMBER_LEGACY_DRIVER = False
    return [pid, pid2]


def p52_streaming_mock():
    from backend.agent.runtime import AgentRuntime
    from backend.core.config import LLMConfig
    from backend.members.member_tools import (
        MEMBER_TOOLS,
        is_parallel_safe,
        is_terminal,
        tool_call_for_action,
    )
    from backend.members.mock_adapter import MockAdapter

    class Writer:
        def __init__(self):
            self.items = []

        @property
        def session_id(self):
            return "probe"

        def append(self, event_key, kind, payload):
            item = {"sequence": len(self.items) + 1, "event_key": event_key,
                    "kind": kind, "payload": payload}
            self.items.append(item)
            return item

        def events(self, after, limit):
            return [i for i in self.items if i["sequence"] > after][:limit]

        def save_checkpoint(self, checkpoint):
            return None

    class Exec:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments, *, idempotency_key):
            self.calls.append((name, arguments))
            return {"status": "ok"}

    script = [{"action": "bash", "command": "id", "thought": "whoami"},
              {"action": "done", "reason": "finished"}]
    adapter = MockAdapter(LLMConfig(api_format="mock"), script=script)
    executor = Exec()
    writer = Writer()
    runtime = AgentRuntime(
        writer, adapter, executor, system="solve", tools=list(MEMBER_TOOLS),
        parallel_safe=is_parallel_safe, is_terminal=is_terminal,
    )
    result = runtime.run("start")
    expected = [tool_call_for_action(a) for a in script]
    check("P5.2 mock turn emits one native tool call per scripted action",
          result.status == "idle" and executor.calls == [
              (name, args) for name, args in expected
          ], f"calls={[n for n, _ in executor.calls]}")
    assistant = [
        m for item in writer.items if item["kind"] == "provider_messages"
        for m in item["payload"]["messages"] if m["role"] == "assistant"
    ]
    check("P5.2 assistant messages carry the provider tool_calls shape",
          bool(assistant) and assistant[0]["tool_calls"][0]["function"]["name"] == "bash")

    idle = MockAdapter(LLMConfig(api_format="mock"))
    result2 = AgentRuntime(
        Writer(), idle, Exec(), system="solve", tools=list(MEMBER_TOOLS),
        parallel_safe=is_parallel_safe, is_terminal=is_terminal,
    ).run("hello")
    check("P5.2 scriptless mock ends idle without inventing actions",
          result2.status == "idle" and "hello" in result2.text)


def p53_adapter_without_turn_falls_back(db, tmp):
    from backend.core.config import MemberConfig
    from backend.members.adapters import MemberAction
    from backend.members.factory import create_member

    pid, iid = _project(db, "decideonly")
    deps = _member_deps(db, tmp / "decideonly")
    member = create_member(MemberConfig(name="jade", api_format="mock"), deps)

    class DecideOnly:
        def decide(self, context):
            return MemberAction.from_obj({"action": "done", "reason": "no turn surface"})

    member.adapter = DecideOnly()
    result = member.solve(pid, iid, "web", is_initial=False)
    entries = deps.logger.read_log("project", pid, None)
    driver = [e for e in entries if e["event"] == "member_driver"]
    check("P5.3 decide-only adapter falls back observably",
          result.status == "done" and len(driver) == 1
          and driver[0]["driver"] == "legacy_fallback"
          and driver[0]["reason"] == "adapter_without_turn_surface",
          driver[0]["reason"] if driver else "none")
    return [pid]


def cleanup(db, pids):
    with db.connect() as conn:
        for pid in pids:
            conn.execute("DELETE FROM projects WHERE id = %s", (pid,))
            conn.execute(
                "DELETE FROM agent_sessions WHERE task_key LIKE %s",
                (f"member:{pid}:%",),
            )


def main():
    from backend.persistence.database import Database

    db = Database().configure()
    pids: list[str] = []
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="p5probe-"))
    try:
        for name, fn in (
            ("p51", lambda: pids.extend(p51_standalone_streaming_and_equivalence(db, tmp))),
            ("p52", p52_streaming_mock),
            ("p53", lambda: pids.extend(p53_adapter_without_turn_falls_back(db, tmp))),
        ):
            try:
                fn()
            except Exception as exc:
                _recovery_logger.debug('Error handled in main', exc_info=True)
                check(f"{name} ran without crashing", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            cleanup(db, pids)
        except Exception as exc:
            _recovery_logger.debug('Error handled in main', exc_info=True)
            print(f"[WARN] probe cleanup failed: {exc}")
        db.close()
    passed = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
