"""P3 docker E2E probe: run inside ipc-app against the compose Postgres.

Proves the deployed image's P3 behaviour deterministically:
  1. GZCTF remote/container detection + concurrent limit.
  2. policy ordering by solve_count then remote-first.
  3. the recon-consumption fallback (建議3): stranded recon degrades into the
     receiver project's blackboard with a competition.message_undelivered event,
     and a later cursor advance emits competition.message_consumed.

Creates everything under a throwaway workflow id and deletes it (cascade) at the
end, so it leaves the competition DB as it found it.
"""
from __future__ import annotations

import sys

from psycopg.types.json import Jsonb

from backend.blackboard import graph_store
from backend.competition.service import CompetitionService
from backend.competition.store import CompetitionStore, new_id
from backend.persistence.database import Database
from backend.platform.gzctf.adapter import GZCTFAdapter

WF = "p3-e2e-probe"
results: list[tuple[str, bool, str]] = []

_RUN_CHILDREN = [
    "competition_run_observations", "competition_messages",
    "competition_submissions", "competition_instances",
    "competition_run_events", "competition_questions",
    "competition_assignments", "competition_wp_jobs",
    "competition_sessions", "competition_run_challenges",
    "competition_challenges",
]


def _cleanup(db):
    """Delete every probe row children-first so the workflow FK can be dropped.

    The deployed app runs its own competition coordinator, which will tick this
    probe's run and keep writing run_events.  Park the run in a terminal state
    first so ``active_run`` stops returning it, give the in-flight tick a moment
    to drain, then delete.
    """
    import time
    with db.connect() as c:
        c.execute(
            "UPDATE competition_runs SET status='stopped' WHERE workflow_id=%s", (WF,)
        )
    time.sleep(2.0)
    with db.connect() as c:
        run_ids = [r["id"] for r in c.execute(
            "SELECT id FROM competition_runs WHERE workflow_id=%s", (WF,)
        ).fetchall()]
        if run_ids:
            session_ids = [r["id"] for r in c.execute(
                "SELECT id FROM competition_sessions WHERE run_id = ANY(%s)", (run_ids,)
            ).fetchall()]
            for table in ("competition_session_events", "agent_session_writers",
                          "agent_events"):
                if session_ids:
                    c.execute(f"DELETE FROM {table} WHERE session_id = ANY(%s)", (session_ids,))
            if session_ids:
                c.execute("DELETE FROM competition_message_cursors WHERE session_id = ANY(%s)",
                          (session_ids,))
            ch_ids = [r["id"] for r in c.execute(
                """SELECT c.id FROM competition_challenges c
                   JOIN competition_run_challenges rc ON rc.challenge_id=c.id
                   WHERE rc.run_id = ANY(%s)""", (run_ids,)
            ).fetchall()]
            if ch_ids:
                c.execute("DELETE FROM competition_artifacts WHERE challenge_id = ANY(%s)", (ch_ids,))
            for table in _RUN_CHILDREN:
                c.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (run_ids,))
            if session_ids:
                c.execute("DELETE FROM agent_sessions WHERE id = ANY(%s)", (session_ids,))
            c.execute("DELETE FROM competition_runs WHERE workflow_id=%s", (WF,))
        c.execute("DELETE FROM workflows WHERE id=%s", (WF,))


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def gzctf_detection():
    from backend.platform.mapping import FieldMapping

    class _ContainerClient:
        base_url = "https://gz.example"
        timeout = 3
        def list_challenges(self, game_id):
            return [
                {"id": 21, "name": "Dyn web", "category": "Web",
                 "type": "DynamicContainer", "containerExposePort": 80, "solved": 42},
                {"id": 22, "name": "Static misc", "category": "Misc",
                 "type": "StaticAttachment", "files": [{"url": "/files/m.zip"}],
                 "solved": 130},
            ]
        def get_profile(self):
            return {"userName": "fixture"}
        def get_public_scoreboard(self, game_id):
            return {"challenges": {}}
        def get_challenge(self, game_id, challenge_id):
            return {"isSolved": False}
        def get_game(self, game_id):
            return {"id": game_id, "concurrentContainerLimit": 1}
        def create_container(self, g, c): return {}
        def extend_container(self, g, c): return {}
        def delete_container(self, g, c): return None

    mapping = FieldMapping(platform="gzctf", game_id=7, level=1,
                           level_field="level", track_id_field="trackId")
    adapter = GZCTFAdapter(_ContainerClient(), mapping, max_attachment_bytes=1024)
    challenges = {item.external_id: item for item in adapter.preflight()}
    container = challenges["21"]
    static = challenges["22"]
    check("gzctf remote detects DynamicContainer", container.remote is True,
          f"remote={container.remote}")
    check("gzctf static not remote", static.remote is False, f"remote={static.remote}")
    check("gzctf solve_count parsed",
          container.platform_data.get("solve_count") == 42
          and static.platform_data.get("solve_count") == 130,
          f"{container.platform_data.get('solve_count')}/{static.platform_data.get('solve_count')}")
    check("gzctf container_type recorded",
          container.platform_data.get("container_type") == "DynamicContainer"
          and "container_type" not in static.platform_data,
          f"{container.platform_data.get('container_type')}")
    check("gzctf instance limit read from platform",
          adapter.instance_limit() == 1, f"limit={adapter.instance_limit()}")


def policy_ordering():
    from datetime import datetime, timezone

    from backend.competition.policy import Challenge, plan_assignments

    now = datetime.now(timezone.utc)
    easy_remote = Challenge(id="er", category="web", ready_at=now, remote=True,
                            instance_ready=True, solve_count=50)
    easy_local = Challenge(id="el", category="misc", ready_at=now, remote=False,
                           solve_count=50)
    hard_remote = Challenge(id="hr", category="pwn", ready_at=now, remote=True,
                            instance_ready=True, solve_count=2)
    unreported = Challenge(id="ur", category="misc", ready_at=now, remote=False,
                           solve_count=None)
    plan = plan_assignments(
        [easy_local, hard_remote, easy_remote, unreported],
        occupied_members=set(), remote_capacity=5, now=now, local_capacity=10,
    )
    order = [a.challenge_id for a in plan if a.role == "primary"]
    check("policy orders high-solve before low-solve",
          order.index("er") < order.index("hr") and order.index("el") < order.index("hr"),
          f"order={order}")
    check("policy prefers remote at equal solve_count",
          order.index("er") < order.index("el"), f"order={order}")
    check("policy sorts unreported solve_count last",
          order.index("ur") > order.index("hr"), f"order={order}")


def message_fallback(db):
    store = CompetitionStore(db)
    _cleanup(db)
    with db.connect() as c:
        c.execute(
            """INSERT INTO workflows (id,source,name,spec_json,spec_digest,created_at,updated_at)
               VALUES (%s,'test','p3 probe','{}','digest',now(),now())
               ON CONFLICT (id) DO NOTHING""", (WF,),
        )
    run = store.create_run(WF, "platform/game/team", f"{WF}-start", {})
    run = store.transition_run(run["id"], "importing", revision=run["revision"])
    run = store.transition_run(run["id"], "running", revision=run["revision"])

    challenge = store.register_challenge(run["id"], "probe-c1", {})
    store.mark_ready(challenge["id"])
    with db.connect() as c:
        project_id = graph_store.create_project(
            c, "Probe challenge", "fixture://probe", "capture the flag", "pwn"
        )
    store.link_project(challenge["id"], project_id)
    assignment = store.assign(run["id"], challenge["id"], "jade", "owner", role="primary")
    session_id = assignment["session_id"]

    envelope = {
        "run_id": run["id"], "challenge_id": challenge["id"], "message_type": "recon",
        "sender_member_id": "topaz", "receiver_member_id": "jade",
        "receiver_session_id": session_id,
        "payload": {"kind": "difficulty_report", "report": {
            "member": "topaz",
            "progress": "Exploit verified locally; blocked on the dynamic instance.",
            "knowledge": ["sign.bin is a 64-bit PIE ELF with no canary."],
            "directions": ["Wait for the queued instance then run solve.py."],
        }},
    }
    with db.connect() as c:
        row = c.execute(
            """INSERT INTO competition_messages
               (run_id,challenge_id,request_id,envelope,status,created_at)
               VALUES (%s,%s,%s,%s,'queued',now()-interval '600 seconds')
               RETURNING sequence""",
            (run["id"], challenge["id"], new_id(), Jsonb(envelope)),
        ).fetchone()
    sequence = int(row["sequence"])

    from types import SimpleNamespace
    class _Log:
        def project(self, *a, **k): pass
        def read_log(self, *a, **k): return []
    class _Cfg:
        member = None
        limits = SimpleNamespace(max_concurrent_tasks=10)
        @staticmethod
        def startup_errors(): return []
        @staticmethod
        def available_members(): return [SimpleNamespace(name=n) for n in ("topaz","jade")]
    state = SimpleNamespace(db=db, instance_id="p3-probe", config=_Cfg(),
                            logger=_Log(), orchestrator=None,
                            artifact_root=None, wp_dir=None)
    svc = CompetitionService(state, tick_interval=0.01)
    svc._message_delivery_window = 0
    try:
        svc._check_message_delivery(run)
        undelivered = [e for e in store.run_events(run["id"])
                       if e["kind"] == "competition.message_undelivered"]
        check("fallback emits message_undelivered", len(undelivered) == 1,
              f"count={len(undelivered)}")
        with db.connect() as c:
            hints = graph_store.list_hints(c, project_id)
        text = "\n".join(h.content for h in hints)
        check("fallback injects teammate hints", "no canary" in text and "solve.py" in text,
              f"hints={len(hints)}")

        store.advance_message_cursor(session_id, sequence)
        svc._check_message_delivery(run)
        consumed = [e for e in store.run_events(run["id"])
                    if e["kind"] == "competition.message_consumed"]
        check("cursor advance emits message_consumed", len(consumed) == 1,
              f"count={len(consumed)}")
    finally:
        svc.shutdown()
        _cleanup(db)


def main():
    db = Database().configure()
    try:
        gzctf_detection()
        policy_ordering()
        message_fallback(db)
    finally:
        db.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results)-len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
