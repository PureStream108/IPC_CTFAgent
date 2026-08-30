from __future__ import annotations

"""Batch automation for ret2shell solving.

Three modes (composable):

  --start              Start projects (those over capacity queue automatically)
  --watch              Poll solved flags and print them as they appear
  --submit             Submit unresolved flags via the backend platform-verdict
                       API (default is a dry-run report unless --no-dry-run)

Platform submission is owned by the backend verdict worker: a project only
reaches "completed" after the platform accepts its flag, and rejections feed
back into member context automatically.

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


def http(method: str, path: str, session: str, *, timeout: int = 120, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=data,
        headers={
            "Cookie": f"ipc_session={session}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


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
    """Submit unresolved flags through the backend verdict API.

    The backend owns platform submission (rate limiting, dedup by
    (project, flag), verdict feedback), so this script only relays flags and
    prints the platform verdicts.
    """
    flags = [f for f in http("GET", "/api/flags", args.session) if f["flag"]]
    pending = [
        f
        for f in flags
        if f["external_id"] and f.get("verdict") in (None, "rejected", "unknown")
    ]
    print(f"flags found: {len(flags)}, unresolved: {len(pending)}")
    if not pending:
        return 0
    rc = 0
    for record in pending:
        label = f"{record['title']} (challenge {record['external_id']})"
        if not args.no_dry_run:
            print(f"[dry-run] would submit {record['flag']!r} for {label}")
            continue
        try:
            result = http(
                "POST",
                f"/api/flags/{record['project_id']}/submit",
                args.session,
                timeout=90,
                body={"flag": record["flag"]},
            )
            print(f"[submit] {label} -> {result}", flush=True)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            print(f"[error] {label}: HTTP {exc.code} {detail}", flush=True)
            rc = 1
    return rc


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


def fetch_descriptions(session: str, projects: list[dict]) -> dict[str, str]:
    """project_id -> origin fact text, for instance-need classification."""
    descs: dict[str, str] = {}
    for p in projects:
        try:
            detail = http("GET", f"/projects/{p['id']}", session, timeout=60)
            detail = detail.get("detail", detail)
            if isinstance(detail, str):
                continue
            descs[p["id"]] = " ".join(
                f.get("description", "") for f in detail.get("facts", [])
            )
        except Exception:
            continue
    return descs


INSTANCE_MARKS = ("nc ", "ncat", "ssh ", "wsrx", "实例", "instance", "远程环境", "连接题目环境")


def needs_instance(project: dict, descriptions: dict[str, str], env_flags: dict[str, bool]) -> bool:
    ext = str(project.get("external_id"))
    if ext in env_flags:
        return env_flags[ext]  # authoritative: the platform's env endpoint
    if project["category"] == "pwn":
        return True
    text = (descriptions.get(project["id"], "") + " " + project["title"]).lower()
    return any(mark in text for mark in INSTANCE_MARKS)


def cmd_auto(args) -> int:
    """Full automation with controlled dispatch: keep at most --slots projects
    running, promoting the next-easiest challenge whenever a slot frees.

    Flag submission is handled by the backend verdict worker: a project only
    completes after the platform accepts its flag, and rejections reopen the
    project with feedback injected."""

    from backend.platform.ret2shell import Ret2ShellClient

    projects = http("GET", "/projects", args.session)
    client = Ret2ShellClient(
        base_url=os.getenv("IPC_R2S_BASE_URL", ""),
        game_id=int(os.getenv("IPC_R2S_GAME_ID") or 37),
        username=os.getenv("IPC_R2S_USERNAME", ""),
        password=os.getenv("IPC_R2S_PASSWORD", ""),
    )
    # Platform ground truth for every project: challenges already solved are
    # skipped regardless of local state, and local "completed" projects whose
    # challenge is NOT solved (legacy runs predate platform adjudication) are
    # reopened so they re-enter this run's queue instead of being stranded.
    solved_ext: set[str] = set()
    for p in projects:
        ext = p.get("external_id")
        if ext is None:
            continue
        try:
            if client.challenge_status(int(ext)).get("solved"):
                solved_ext.add(str(ext))
        except Exception:
            continue
    print(f"platform reports {len(solved_ext)} challenges already solved by this account", flush=True)
    reopened: list[str] = []
    for p in projects:
        ext = str(p.get("external_id") or "")
        if p["status"] == "completed" and ext and ext not in solved_ext:
            try:
                http("POST", f"/projects/{p['id']}/reopen", args.session, timeout=30)
                reopened.append(p["id"])
            except Exception as exc:
                print(f"[reopen] {p['id']} failed: {exc}", flush=True)
    if reopened:
        print(f"reopened {len(reopened)} completed-but-unsolved projects: {reopened}", flush=True)
        projects = http("GET", "/projects", args.session)
    todo = [
        p for p in projects
        if p["status"] not in ("completed", "running", "queued")
        and str(p.get("external_id") or "") not in solved_ext
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
    descriptions = fetch_descriptions(args.session, todo)
    # Authoritative instance classification: ask the platform's env endpoint
    # for every challenge (null = plain challenge, images = dynamic instance).
    env_flags: dict[str, bool] = {}
    for p in todo:
        ext = p.get("external_id")
        if ext is None:
            continue
        try:
            env_flags[str(ext)] = client.has_environment(int(ext))
        except Exception:
            pass
    env_count = sum(1 for v in env_flags.values() if v)
    print(f"platform reports {env_count}/{len(todo)} challenges with dynamic environments", flush=True)
    instance_todo = [p for p in todo if needs_instance(p, descriptions, env_flags)]
    normal_todo = [p for p in todo if not needs_instance(p, descriptions, env_flags)]
    print(
        f"auto: {len(todo)} projects ({len(normal_todo)} normal + {len(instance_todo)} "
        f"instance-type, at most 1 instance project running), {args.slots} slots",
        flush=True,
    )
    for p in todo[:10]:
        ext = str(p.get("external_id"))
        print(f"  {p['id']} [{p['category']}] {p['title']} solves={ranking.get(ext, ('?',))[0]}", flush=True)

    normal_index = 0
    instance_index = 0
    instance_started: dict[str, str] = {}  # project_id -> title
    with client:
        while True:
            # --- instance reconciliation: destroy instances of finished projects ---
            try:
                projects_now = http("GET", "/projects", args.session, timeout=60)
                status_by_ext = {
                    p.get("external_id"): p["status"] for p in projects_now
                }
                for inst in client.list_instances():
                    ext = str(inst.get("challenge_id"))
                    if status_by_ext.get(ext) not in ("running", "queued"):
                        try:
                            client.destroy_instance(int(ext))
                            print(f"[instance] released finished challenge {ext}", flush=True)
                        except Exception as exc:
                            print(f"[instance] release failed for {ext}: {exc}", flush=True)
            except Exception as exc:
                print(f"[reconcile] failed: {exc}", flush=True)
                projects_now = http("GET", "/projects", args.session, timeout=60)

            # --- dispatch: normal projects fill slots; at most ONE instance ---
            # type project runs at a time (platform quota is one instance/team).
            try:
                runtime = http("GET", "/config/runtime", args.session, timeout=30)
                active = len(runtime["limiter"]["active_tasks"])
            except Exception as exc:
                print(f"[dispatch] runtime query failed: {exc}", flush=True)
                active = 0
            instance_running = any(
                next((p["status"] for p in projects_now if p["id"] == pid), "completed")
                == "running"
                for pid in instance_started
            )
            # Hard gate on the platform's ground truth: classification can
            # miss challenges (or a member may start one anyway), so never
            # dispatch an instance project while ANY instance is alive.
            try:
                platform_instances = client.list_instances()
                if platform_instances:
                    instance_running = True
            except Exception as exc:
                print(f"[dispatch] instance query failed: {exc}", flush=True)
            while active < args.slots and normal_index < len(normal_todo):
                project = normal_todo[normal_index]
                normal_index += 1
                try:
                    http("POST", f"/projects/{project['id']}/start", args.session, timeout=60)
                    print(
                        f"[start] {project['id']} [{project['category']}] {project['title']} "
                        f"(slot {active + 1}/{args.slots}, normal {normal_index}/{len(normal_todo)})",
                        flush=True,
                    )
                    active += 1
                    time.sleep(5)  # stagger container startup
                except urllib.error.HTTPError as exc:
                    print(f"[start] {project['id']} failed: HTTP {exc.code}", flush=True)
            if (
                not instance_running
                and instance_index < len(instance_todo)
                and active < args.slots
            ):
                project = instance_todo[instance_index]
                instance_index += 1
                try:
                    http("POST", f"/projects/{project['id']}/start", args.session, timeout=60)
                    instance_started[project["id"]] = project["title"]
                    print(
                        f"[start] {project['id']} [{project['category']}] {project['title']} "
                        f"(INSTANCE-TYPE {instance_index}/{len(instance_todo)}, slot {active + 1}/{args.slots})",
                        flush=True,
                    )
                    active += 1
                except urllib.error.HTTPError as exc:
                    print(f"[start] {project['id']} failed: HTTP {exc.code}", flush=True)

            # --- flag submission is owned by the backend verdict worker ---
            flags = http("GET", "/api/flags", args.session)
            accepted = sum(1 for f in flags if f.get("verdict") == "solved")
            awaiting = sum(1 for f in flags if f["status"] == "pending_verdict")
            print(
                f"... dispatched={normal_index + instance_index}/{len(todo)} "
                f"(normal {normal_index}/{len(normal_todo)}, instance {instance_index}/{len(instance_todo)}) "
                f"flags_found={sum(1 for f in flags if f['flag'])} "
                f"awaiting_verdict={awaiting} platform_accepted={accepted} (Ctrl+C to stop)",
                flush=True,
            )
            if (
                normal_index >= len(normal_todo)
                and instance_index >= len(instance_todo)
                and active == 0
            ):
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
