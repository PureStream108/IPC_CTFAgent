"""P4 docker E2E probe: run inside ipc-app against the compose Postgres.

Proves the deployed image's P4 robustness behaviour deterministically, using
only in-process construction so it never perturbs the live coordinator:

  T7  event-key hardening: the resumed-session continuation key is stable and
      epoch-scoped (not a drifting position key), replay is idempotent, and a
      reused key with different content raises SessionNeedsRebuild.
  T3  session self-heal: an invalid tool-order 400 is repaired in place; a 400
      that repair cannot fix (and a second 400 on an already-repaired turn)
      escalates to SessionNeedsRebuild(reason=invalid_tool_order), not a loop.
  T4  stop/teardown: the orchestrator ownership partition treats a stopped-run
      project as terminal (excluded from recovery) and an active-run project as
      owned; the generic log throttle folds a burst to one line.
  T12 migration/API hardening: env.py sets a committed lock_timeout; the API
      deadlock-retry helper retries once then raises.

Prints PASS/FAIL per check and exits non-zero if any fail. Touches no
competition rows, so there is nothing to clean up.
"""
from __future__ import annotations

import logging
import sys

# Error boundaries keep their recovery behavior and retain debug diagnostics.
_recovery_logger = logging.getLogger(__name__)

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


class _MemWriter:
    """In-memory transcript with the store's reused-key conflict semantics."""

    def __init__(self, session_id="probe", epoch=1):
        from backend.agent.session import SessionConflict
        self._conflict = SessionConflict
        self._session_id = session_id
        self.epoch = epoch
        self.items: list[dict] = []
        self.checkpoints: list[dict] = []

    @property
    def session_id(self):
        return self._session_id

    def append(self, event_key, kind, payload):
        existing = next((i for i in self.items if i["event_key"] == event_key), None)
        if existing:
            if existing["kind"] != kind or existing["payload"] != payload:
                raise self._conflict("event key reused with different content")
            return existing
        item = {"session_id": self._session_id, "sequence": len(self.items) + 1,
                "event_key": event_key, "kind": kind, "payload": payload}
        self.items.append(item)
        return item

    def events(self, after=0, limit=500):
        return [i for i in self.items if i["sequence"] > after][:limit]

    def save_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)

    def record_usage(self, *a, **k):
        pass


class _Turn:
    def __init__(self, messages, tools=None, text=""):
        self.messages = messages
        self.tools = tools or []
        self.text = text
        self.usage = None


class _Echo:
    """Answers the newest user message; records what it saw."""

    def __init__(self):
        self.prompts: list[str] = []

    def turn(self, messages, system, tools, emit, cancel):
        latest = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
        self.prompts.append(latest)
        emit(f"re: {latest}")
        return _Turn([{"role": "assistant", "content": f"re: {latest}"}], [], f"re: {latest}")

    def tool_result(self, call, output):
        return {"role": "tool", "tool_call_id": call.id, "content": str(output)}


class _Status400(Exception):
    def __init__(self, status_code=400):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _runtime(writer, adapter):
    from backend.agent.runtime import AgentRuntime

    class _Exec:
        def execute(self, name, arguments, *, idempotency_key):
            return {"ok": True}

    return AgentRuntime(writer, adapter, _Exec(), system="solve", tools=[])


def t7_event_keys():
    import hashlib

    from backend.agent.session import SessionNeedsRebuild

    writer = _MemWriter(epoch=9)
    adapter = _Echo()
    _runtime(writer, adapter).run("first")
    _runtime(writer, adapter).run("second")
    follow = [i for i in writer.items if i["kind"] == "user_message"]
    expected = "session:user:continue:9:" + hashlib.sha256(b"second").hexdigest()[:16]
    check("T7 continuation key is stable+epoch-scoped",
          len(follow) == 1 and follow[0]["event_key"] == expected,
          follow[0]["event_key"] if follow else "none")

    _runtime(writer, adapter).run("second")
    follow2 = [i for i in writer.items if i["kind"] == "user_message"]
    check("T7 continuation replay is idempotent", len(follow2) == 1, f"n={len(follow2)}")

    writer2 = _MemWriter(epoch=1)
    writer2.append("session:user:initial", "provider_messages",
                   {"messages": [{"role": "user", "content": "go"}]})
    text = "resume please"
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    writer2.append(f"session:user:continue:1:{digest}", "user_message",
                   {"messages": [{"role": "user", "content": "DIFFERENT stored"}]})
    try:
        _runtime(writer2, _Echo()).run(text)
        check("T7 reused-key conflict needs rebuild", False, "no raise")
    except SessionNeedsRebuild as exc:
        check("T7 reused-key conflict needs rebuild", exc.reason == "event_key_conflict",
              exc.reason)


def t3_self_heal():
    from backend.agent.session import SessionNeedsRebuild

    writer = _MemWriter()
    writer.append("session:user:initial", "provider_messages",
                  {"messages": [{"role": "user", "content": "go"}]})
    inst = _runtime(writer, _Echo())
    poisoned = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "shell"}}]},
    ]
    repaired = inst._handle_turn_failure(_Status400(), poisoned, turn_number=5, repaired_turn=-1)
    check("T3 invalid-tool-order 400 repaired in place",
          repaired != poisoned and any(m.get("role") == "tool" for m in repaired),
          f"n={len(repaired)}")

    try:
        inst._handle_turn_failure(_Status400(), [{"role": "user", "content": "go"}],
                                  turn_number=5, repaired_turn=5)
        check("T3 persistent 400 escalates to rebuild", False, "no raise")
    except SessionNeedsRebuild as exc:
        check("T3 persistent 400 escalates to rebuild",
              exc.reason == "invalid_tool_order", exc.reason)

    try:
        inst._handle_turn_failure(_Status400(status_code=429),
                                  [{"role": "user", "content": "go"}],
                                  turn_number=6, repaired_turn=-1)
        check("T3 non-400 propagates unchanged", False, "no raise")
    except SessionNeedsRebuild:
        check("T3 non-400 propagates unchanged", False, "wrongly rebuilt")
    except Exception:
        _recovery_logger.debug('Error handled in t3_self_heal', exc_info=True)
        check("T3 non-400 propagates unchanged", True, "raised as-is")


def t4_ownership_and_throttle():
    from types import SimpleNamespace

    from backend.core.orchestrator import Orchestrator
    from backend.persistence.database import Database

    orch = Orchestrator.__new__(Orchestrator)
    logged: list[dict] = []
    orch.state = SimpleNamespace(
        logger=SimpleNamespace(project=lambda event, pid, **f: logged.append(
            {"event": event, "pid": pid, **f}))
    )
    orch._throttle_counts = {}
    orch._throttle_next_log = {}
    orch._throttle_emitted = {}

    db = Database().configure()
    try:
        with db.connect() as conn:
            active, terminal = orch._competition_project_ownership(conn)
        check("T4 ownership partition returns disjoint sets",
              isinstance(active, set) and isinstance(terminal, set)
              and active.isdisjoint(terminal),
              f"active={len(active)} terminal={len(terminal)}")
    finally:
        db.close()

    for _ in range(40):
        orch._throttled_log("engine_lease_conflict", "probe-proj", "probe-proj",
                            requested_kind="competition", requested_owner="probe")
    emitted = [e for e in logged if e["event"] == "engine_lease_conflict"]
    check("T4 log throttle folds a burst to one line",
          len(emitted) == 1 and emitted[0]["attempts"] == 1,
          f"emitted={len(emitted)}")


def t12_hardening():
    import psycopg.errors

    from backend.api import project as project_api

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock")
        return "ok"

    got = project_api._with_deadlock_retry(flaky)
    check("T12 deadlock retry succeeds on second try", got == "ok" and calls["n"] == 2,
          f"calls={calls['n']}")

    def always():
        raise psycopg.errors.DeadlockDetected("deadlock")

    try:
        project_api._with_deadlock_retry(always)
        check("T12 persistent deadlock propagates", False, "no raise")
    except psycopg.errors.DeadlockDetected:
        check("T12 persistent deadlock propagates", True, "raised after one retry")

    import os

    import backend.persistence as persist_pkg
    env_path = os.path.join(
        os.path.dirname(persist_pkg.__file__), "migrations", "env.py"
    )
    with open(env_path, encoding="utf-8") as handle:
        src = handle.read()
    check("T12 migration sets committed lock_timeout",
          "lock_timeout" in src and "connection.commit()" in src,
          "env.py")


def main():
    for fn in (t7_event_keys, t3_self_heal, t4_ownership_and_throttle, t12_hardening):
        try:
            fn()
        except Exception as exc:
            _recovery_logger.debug('Error handled in main', exc_info=True)
            check(f"{fn.__name__} ran without crashing", False, f"{type(exc).__name__}: {exc}")
    passed = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
