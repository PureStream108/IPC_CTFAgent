from __future__ import annotations

"""Batch automation for ret2shell solving.

Three modes (composable):

  --start              Start projects (those over capacity queue automatically)
  --watch              Poll solved flags and print them as they appear
  --submit             Submit unsent flags to the platform (rate-limited;
                       default is a dry-run report unless --no-dry-run)

Run inside the ipc-app container next to the other ret2shell scripts:

  docker exec ipc-app python /tmp/autosolve.py --session <cookie> --start --category misc --limit 10
  docker exec ipc-app python /tmp/autosolve.py --session <cookie> --watch --interval 15
  docker exec ipc-app python /tmp/autosolve.py --session <cookie> --submit --no-dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
STATE_FILE = Path("/app/data/ret2shell_submissions.json")


def http(method: str, path: str, session: str, *, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={"Cookie": f"ipc_session={session}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body else {}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"submitted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_start(args) -> int:
    projects = http("GET", "/projects", args.session)
    todo = [
        p
        for p in projects
        if p["status"] not in ("completed", "running", "queued")
    ]
    if args.category:
        todo = [p for p in todo if p["category"] == args.category]
    if args.limit:
        todo = todo[: args.limit]
    print(f"starting {len(todo)} projects (queue picks them up as slots free)")
    started = 0
    for project in todo:
        try:
            result = http("POST", f"/projects/{project['id']}/start", args.session)
            print(f"  {project['id']} [{project['category']}] {project['title']} -> {result.get('phase')}")
            started += 1
        except urllib.error.HTTPError as exc:
            print(f"  {project['id']} start failed: HTTP {exc.code}")
    print(f"started {started}/{len(todo)}")
    return 0


def cmd_watch(args) -> int:
    seen: set[str] = set()
    print("watching for flags (Ctrl+C to stop)...")
    try:
        while True:
            flags = http("GET", "/api/flags", args.session)
            for record in flags:
                if record["flag"] and record["project_id"] not in seen:
                    seen.add(record["project_id"])
                    print(
                        f"[flag] {record['title']} (challenge {record['external_id']}) "
                        f"{record['flag']} status={record['status']}",
                        flush=True,
                    )
            running = sum(1 for f in flags if f["status"] in ("running", "queued"))
            if not running and seen:
                print("no active projects remain; watch exiting")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_submit(args) -> int:
    from backend.platform.ret2shell import (
        Ret2ShellClient,
        Ret2ShellError,
        Ret2ShellPreflightError,
        Ret2ShellRateLimitError,
    )

    flags = [f for f in http("GET", "/api/flags", args.session) if f["flag"]]
    state = load_state()
    submitted = state["submitted"]
    pending = [f for f in flags if f["external_id"] and f["external_id"] not in submitted]
    print(f"flags found: {len(flags)}, unsent: {len(pending)}")
    if not pending:
        return 0

    client = Ret2ShellClient(
        base_url=os.getenv("IPC_R2S_BASE_URL", ""),
        game_id=int(os.getenv("IPC_R2S_GAME_ID") or 37),
        username=os.getenv("IPC_R2S_USERNAME", ""),
        password=os.getenv("IPC_R2S_PASSWORD", ""),
    )
    with client:
        for record in pending:
            challenge_id = int(record["external_id"])
            label = f"{record['title']} (challenge {challenge_id})"
            if not args.no_dry_run:
                print(f"[dry-run] would submit {record['flag']!r} for {label}")
                submitted[record["external_id"]] = {"flag": record["flag"], "dry_run": True}
                save_state(state)
                continue
            try:
                status = client.challenge_status(challenge_id)
                if isinstance(status, dict) and status.get("solved"):
                    print(f"[skip] {label} already solved on platform")
                    submitted[record["external_id"]] = {"flag": record["flag"], "solved": "platform"}
                    save_state(state)
                    continue
                result = client.submit_flag(challenge_id, record["flag"], check_solved=False)
                solved = result.get("solved")
                print(
                    f"[submit] {label} {record['flag']!r} -> "
                    f"solved={solved} result={result.get('result')}"
                )
                submitted[record["external_id"]] = {
                    "flag": record["flag"],
                    "submission_id": result.get("id"),
                    "solved": solved,
                }
                save_state(state)
            except Ret2ShellPreflightError as exc:
                print(f"[refused] {label}: {exc}")
            except Ret2ShellRateLimitError as exc:
                print(f"[rate-limit] stopping batch: {exc}")
                return 1
            except Ret2ShellError as exc:
                print(f"[error] {label}: {exc}")
    return 0


def cmd_auto(args) -> int:
    """Full automation: start every remaining project, then loop — submit
    each new flag to the platform as it appears, until nothing is left
    running or queued.  Starting beyond capacity is fine: the orchestrator
    queues projects and starts them as slots free up."""

    from backend.platform.ret2shell import (
        Ret2ShellClient,
        Ret2ShellError,
        Ret2ShellPreflightError,
        Ret2ShellRateLimitError,
    )

    projects = http("GET", "/projects", args.session)
    todo = [
        p
        for p in projects
        if p["status"] not in ("completed", "running", "queued")
    ]
    if args.category:
        todo = [p for p in todo if p["category"] == args.category]
    print(f"auto mode: starting {len(todo)} projects (capacity queue handles the rest)")
    for project in todo:
        try:
            http("POST", f"/projects/{project['id']}/start", args.session)
            print(f"  started {project['id']} [{project['category']}] {project['title']}", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"  {project['id']} start failed: HTTP {exc.code}", flush=True)

    state = load_state()
    client = Ret2ShellClient(
        base_url=os.getenv("IPC_R2S_BASE_URL", ""),
        game_id=int(os.getenv("IPC_R2S_GAME_ID") or 37),
        username=os.getenv("IPC_R2S_USERNAME", ""),
        password=os.getenv("IPC_R2S_PASSWORD", ""),
    )
    with client:
        while True:
            flags = http("GET", "/api/flags", args.session)
            projects_now = http("GET", "/projects", args.session)
            active = sum(1 for p in projects_now if p["status"] in ("running", "queued"))
            for record in flags:
                ext = record["external_id"]
                if not (record["flag"] and ext) or ext in state["submitted"]:
                    continue
                challenge_id = int(ext)
                label = f"{record['title']} (challenge {challenge_id})"
                try:
                    status = client.challenge_status(challenge_id)
                    if isinstance(status, dict) and status.get("solved"):
                        print(f"[skip] {label} already solved on platform", flush=True)
                        state["submitted"][ext] = {"flag": record["flag"], "solved": "platform"}
                        save_state(state)
                        continue
                    result = client.submit_flag(challenge_id, record["flag"], check_solved=False)
                    solved = result.get("solved")
                    print(
                        f"[submit] {label} {record['flag']!r} -> solved={solved} result={result.get('result')}",
                        flush=True,
                    )
                    state["submitted"][ext] = {
                        "flag": record["flag"],
                        "submission_id": result.get("id"),
                        "solved": solved,
                        "result": result.get("result"),
                    }
                    save_state(state)
                except Ret2ShellPreflightError as exc:
                    print(f"[refused] {label}: {exc}", flush=True)
                    state["submitted"][ext] = {"flag": record["flag"], "refused": str(exc)}
                    save_state(state)
                except Ret2ShellRateLimitError as exc:
                    print(f"[rate-limit] backing off this cycle: {exc}", flush=True)
                    break
                except Ret2ShellError as exc:
                    print(f"[error] {label}: {exc}", flush=True)
            score = sum(
                1 for v in state["submitted"].values() if v.get("solved") is True
            )
            print(
                f"... active={active} flags_found={sum(1 for f in flags if f['flag'])} "
                f"platform_accepted={score} (Ctrl+C to stop)",
                flush=True,
            )
            if active == 0:
                print("all projects finished; auto mode exiting")
                return 0
            time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="admin ipc_session cookie")
    parser.add_argument("--auto", action="store_true", help="start all + auto-submit loop")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--category", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--no-dry-run", action="store_true")
    args = parser.parse_args()
    if args.auto:
        return cmd_auto(args)
    if not (args.start or args.watch or args.submit):
        parser.error("choose at least one of --start / --watch / --submit / --auto")
    if args.start:
        cmd_start(args)
    if args.watch:
        return cmd_watch(args)
    if args.submit:
        return cmd_submit(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
