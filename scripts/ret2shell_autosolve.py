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


PLATFORM_FLAG_PREFIX = "moectf"


def normalize_flag_prefix(flag: str) -> tuple[str, bool]:
    """The platform enforces a single flag prefix (moectf{...}). Members may
    copy a literal ctf{...}/flag{...} string out of tutorial material, so the
    prefix is rewritten before submission."""
    import re

    match = re.match(r"^[A-Za-z0-9_-]+\{(.+)\}$", flag.strip())
    if match and not flag.strip().lower().startswith(PLATFORM_FLAG_PREFIX + "{"):
        return f"{PLATFORM_FLAG_PREFIX}{{{match.group(1)}}}", True
    return flag.strip(), False


def record_feedback(project_id: str, title: str, flag: str, verdict: str) -> None:
    """Feed the platform's verdict back to the agents via the persistent
    experience memory, which every Member's memory search surfaces."""
    try:
        from backend.memory.memory_store import MemoryStore

        root = os.environ.get("IPC_ROOT", "/app")
        memory = MemoryStore(Path(root) / "data" / "memory")
        memory.add(
            "misc",
            "Platform flag format feedback",
            (
                f"Platform rejected flag {flag!r} for {title!r}: {verdict}. "
                f"All submissions must use the {PLATFORM_FLAG_PREFIX}{{...}} prefix. "
                "When you find a flag with a different prefix in challenge material, "
                f"report it as {PLATFORM_FLAG_PREFIX}{{original-inner-content}}."
            ),
            tags=["flag-format", "submission", "ret2shell"],
            project_id=project_id,
            source="autosolve",
        )
        print(f"[feedback] memory entry recorded for {title!r}", flush=True)
    except Exception as exc:  # never break the submit loop on feedback
        print(f"[feedback] failed to record: {exc}", flush=True)


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


CATEGORY_DIFFICULTY = {"misc": 0, "web": 1, "crypto": 2, "ai": 3, "reverse": 4, "pwn": 5}
EASY_TITLE_MARKS = ("入门", "签到", "如何", "开始", "ez_", "ez-", "warmup")


def load_ranking() -> dict[str, tuple[int, int, int]]:
    """external_id -> (solves, category_rank, score); higher solves = easier."""
    targets = Path("/app/platform_targets/ret2shell.yaml")
    ranking: dict[str, tuple[int, int, int]] = {}
    if not targets.exists():
        return ranking
    import yaml

    data = yaml.safe_load(targets.read_text(encoding="utf-8")) or {}
    for ch in data.get("challenges", []):
        ext = str(ch.get("id"))
        solves = ch.get("total_solves") or 0
        category = str(ch.get("category", "misc"))
        score = ch.get("score", 999)
        ranking[ext] = (int(solves), CATEGORY_DIFFICULTY.get(category, 3), int(score))
    return ranking


def rank_projects(projects: list[dict], ranking: dict) -> list[dict]:
    def sort_key(p):
        ext = str(p.get("external_id"))
        solves, cat_rank, score = ranking.get(ext, (0, 3, 999))
        title_easy = 1 if any(m in p["title"].lower() for m in EASY_TITLE_MARKS) else 0
        # easiest first: most platform solves, easy title, easy category, low score
        return (-solves, -title_easy, cat_rank, score)

    return sorted(projects, key=sort_key)


def cmd_auto(args) -> int:
    """Full automation with controlled dispatch: keep at most --slots projects
    running, promoting the next-easiest challenge whenever a slot frees, and
    submit each new flag as it appears."""

    from backend.platform.ret2shell import (
        Ret2ShellClient,
        Ret2ShellError,
        Ret2ShellPreflightError,
        Ret2ShellRateLimitError,
    )

    projects = http("GET", "/projects", args.session)
    todo = [
        p for p in projects
        if p["status"] not in ("completed", "running", "queued")
    ]
    if args.category:
        todo = [p for p in todo if p["category"] == args.category]
    excluded = {c.strip() for c in (args.exclude_category or "").split(",") if c.strip()}
    if excluded:
        skipped = [p for p in todo if p["category"] in excluded]
        todo = [p for p in todo if p["category"] not in excluded]
        print(f"excluded {len(skipped)} projects in categories: {sorted(excluded)}", flush=True)
    ranking = load_ranking()
    todo = rank_projects(todo, ranking)
    print(
        f"auto: {len(todo)} projects to run, {args.slots} slots, easiest-first order:",
        flush=True,
    )
    for p in todo[:10]:
        ext = str(p.get("external_id"))
        print(f"  {p['id']} [{p['category']}] {p['title']} solves={ranking.get(ext, ('?',))[0]}", flush=True)

    state = load_state()
    client = Ret2ShellClient(
        base_url=os.getenv("IPC_R2S_BASE_URL", ""),
        game_id=int(os.getenv("IPC_R2S_GAME_ID") or 37),
        username=os.getenv("IPC_R2S_USERNAME", ""),
        password=os.getenv("IPC_R2S_PASSWORD", ""),
    )
    next_index = 0
    with client:
        while True:
            # --- dispatch: fill free slots with the next-easiest projects ---
            try:
                runtime = http("GET", "/config/runtime", args.session, timeout=30)
                active = len(runtime["limiter"]["active_tasks"])
            except Exception as exc:
                print(f"[dispatch] runtime query failed: {exc}", flush=True)
                active = 0
            while active < args.slots and next_index < len(todo):
                project = todo[next_index]
                next_index += 1
                try:
                    http("POST", f"/projects/{project['id']}/start", args.session, timeout=60)
                    print(
                        f"[start] {project['id']} [{project['category']}] {project['title']} "
                        f"(slot {active + 1}/{args.slots}, {len(todo) - next_index} left)",
                        flush=True,
                    )
                    active += 1
                    time.sleep(5)  # stagger container startup
                except urllib.error.HTTPError as exc:
                    print(f"[start] {project['id']} failed: HTTP {exc.code}", flush=True)

            # --- submit new flags ---
            flags = http("GET", "/api/flags", args.session)
            for record in flags:
                ext = record["external_id"]
                if not (record["flag"] and ext) or ext in state["submitted"]:
                    continue
                challenge_id = int(ext)
                label = f"{record['title']} (challenge {challenge_id})"
                flag = record["flag"]
                normalized, rewritten = normalize_flag_prefix(flag)
                if rewritten:
                    print(f"[normalize] {label}: {flag!r} -> {normalized!r}", flush=True)
                try:
                    status = client.challenge_status(challenge_id)
                    if isinstance(status, dict) and status.get("solved"):
                        print(f"[skip] {label} already solved on platform", flush=True)
                        state["submitted"][ext] = {"flag": normalized, "solved": "platform"}
                        save_state(state)
                        continue
                    result = client.submit_flag(challenge_id, normalized, check_solved=False)
                    solved = result.get("solved")
                    verdict = str(result.get("result") or "")
                    # The platform tells us the expected prefix when the
                    # format is wrong — honor that feedback immediately.
                    if solved is False and "flag should be" in verdict.lower():
                        fixed = f"{PLATFORM_FLAG_PREFIX}{{{normalized.split('{', 1)[1]}"
                        print(f"[format-fix] retrying {label} with {fixed!r}", flush=True)
                        result = client.submit_flag(challenge_id, fixed, check_solved=False)
                        solved = result.get("solved")
                        verdict = str(result.get("result") or "")
                        normalized = fixed
                    print(
                        f"[submit] {label} {normalized!r} -> solved={solved} result={verdict}",
                        flush=True,
                    )
                    state["submitted"][ext] = {
                        "flag": normalized,
                        "submission_id": result.get("id"),
                        "solved": solved,
                        "result": verdict,
                    }
                    save_state(state)
                    if solved is False:
                        record_feedback(
                            record["project_id"], record["title"], normalized, verdict
                        )
                except Ret2ShellPreflightError as exc:
                    print(f"[refused] {label}: {exc}", flush=True)
                    state["submitted"][ext] = {"flag": normalized, "refused": str(exc)}
                    save_state(state)
                except Ret2ShellRateLimitError as exc:
                    print(f"[rate-limit] backing off this cycle: {exc}", flush=True)
                    break
                except Ret2ShellError as exc:
                    print(f"[error] {label}: {exc}", flush=True)

            accepted = sum(1 for v in state["submitted"].values() if v.get("solved") is True)
            print(
                f"... dispatched={next_index}/{len(todo)} flags_found={sum(1 for f in flags if f['flag'])} "
                f"platform_accepted={accepted} (Ctrl+C to stop)",
                flush=True,
            )
            if next_index >= len(todo) and active == 0:
                print("all projects finished; auto mode exiting")
                return 0
            time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="admin ipc_session cookie")
    parser.add_argument("--auto", action="store_true", help="ranked dispatch + auto-submit loop")
    parser.add_argument("--slots", type=int, default=8, help="max concurrent running projects")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--category", default="")
    parser.add_argument("--exclude-category", default="", help="comma-separated categories to skip in auto mode")
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
