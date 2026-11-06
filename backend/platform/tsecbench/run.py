"""Drive a full TSecBench evaluation with IPC as the solving agent.

    python -m backend.platform.tsecbench.run --token ... --base-url ...

One challenge at a time, the way the platform expects:

    list_challenges -> start_challenge -> IPC project -> poll -> submit_flag -> close

The platform owns containers, flags and scoring; IPC owns the solving team.
This module is the only glue: it never writes to IPC's database, it only calls
the REST API, so leases, epochs and the Pentest board stay authoritative.

Requires BENCHMARK_TOKEN + BENCHMARK_BASE_URL (from the platform's
"获取接入配置") and an active VPN, otherwise the SDK raises VpnCheckError.
The tsec_benchmark SDK is imported inside main(), so installing the backend
never requires the evaluation SDK.

Every step is appended to <log-dir>/run-<stamp>/events.jsonl so a 6h run can be
audited afterwards, and the summary is rewritten after each challenge.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "exports" / "tsecbench"
DEFAULT_API = "http://127.0.0.1:8000"

DENYLIST = [
    "172.19.0.1",
    "172.20.0.1",
    "tsecbench.zc.tencent.com",
    "/internal/mcp",
    "BENCHMARK_TOKEN",
]
CONSTRAINTS = [
    "只允许访问本题容器地址；禁止扫描评测平台与 10.0.0.0/8 以外网段",
    "禁止使用或猜测 BENCHMARK_TOKEN",
    (
        "禁止为找答案上网：不得搜索题名/题号，不得检索 writeup、题解、walkthrough、flag、solution、"
        "别人的解题脚本；不得访问 GitHub 搜索/GitHub API/HuggingFace/grep.app/CTFtime 匹配本题"
    ),
    (
        "只允许三种联网：查 CVE 编号或官方文档、下载题面给出的依赖、确认靶场 banner/版本指纹；"
        "判断不了就不搜，改为读靶场文件与日志"
    ),
    "每次联网命令必须以 NET_REASON: <cve|docs|dependency|fingerprint> 开头",
    "禁止破坏性操作；拿到 flag 立即提交",
]


class RunLog:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.events = directory / "events.jsonl"
        self.summary = directory / "summary.json"
        self.state: dict = {"started_at": now(), "challenges": {}, "submitted": 0, "awarded": 0}

    def event(self, kind: str, **fields) -> None:
        record = {"at": now(), "kind": kind, **fields}
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def flush(self) -> None:
        self.summary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api(method: str, url: str, payload: dict | None = None, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
    return json.loads(body.decode("utf-8")) if body else None


def create_benchmark_workflow(
    api_url: str, base_url: str, token: str, *, remote_concurrency: int = 0
) -> str:
    """Create the fixed TSecBench preset workflow through the ops API."""

    payload = {
        "base_url": base_url,
        "name": f"TSecBench {datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "remote_concurrency": max(0, min(3, int(remote_concurrency))),
        "allow_private_networks": True,
        "secrets": {"benchmark_token": token},
    }
    created = api("POST", f"{api_url.rstrip('/')}/api/ops/workflows/tsecbench", payload)
    workflow_id = str((created or {}).get("id") or "")
    if not workflow_id:
        raise RuntimeError("IPC did not return a workflow id")
    api(
        "POST",
        f"{api_url.rstrip('/')}/api/ops/workflows/{workflow_id}/confirm",
        {"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    )
    return workflow_id


def start_run(api_url: str, workflow_id: str, *, select: list[str] | None = None) -> str:
    payload: dict = {"idempotency_key": f"tsecbench-cli-{uuid4().hex[:12]}"}
    if select:
        payload["select"] = select
    snapshot = api(
        "POST", f"{api_url.rstrip('/')}/api/workflows/{workflow_id}/start", payload
    )
    run_id = str(((snapshot or {}).get("run") or {}).get("id") or "")
    if not run_id:
        raise RuntimeError("IPC did not return a run id")
    return run_id


def run_snapshot(api_url: str, run_id: str) -> dict:
    return api("GET", f"{api_url.rstrip('/')}/api/runs/{run_id}") or {}


def stop_run(api_url: str, run_id: str) -> None:
    revision = int(((run_snapshot(api_url, run_id).get("run") or {}).get("revision")) or 0)
    api("POST", f"{api_url.rstrip('/')}/api/runs/{run_id}/stop", {"revision": revision})


def record_snapshot(log: RunLog, snapshot: dict) -> str:
    challenges = snapshot.get("challenges") or []
    for challenge in challenges:
        code = str(challenge.get("external_id") or "")
        if not code:
            continue
        entry = log.state["challenges"].setdefault(code, {})
        entry["state"] = challenge.get("state")
        entry["project_id"] = challenge.get("project_id")
        entry["finished_at"] = now()
    log.state["submitted"] = sum(1 for item in challenges if item.get("state") == "solved")
    log.state["submitted_at"] = now()
    log.flush()
    return str((snapshot.get("run") or {}).get("status") or "")


def main() -> int:
    """Compatibility entry point.

    The old serial loop created one Project per challenge, started it, polled for
    a flag and submitted it itself.  That is a second execution loop, so this CLI
    now only drives the shared path: create the TSecBench workflow, start one
    CompetitionRun, and follow the run.  Scheduling, instance lifecycle and flag
    submission all belong to CompetitionService.
    """

    parser = argparse.ArgumentParser(description="Run one TSecBench evaluation with IPC as the agent.")
    parser.add_argument("--token", default=os.environ.get("BENCHMARK_TOKEN", ""))
    parser.add_argument("--base-url", default=os.environ.get("BENCHMARK_BASE_URL", ""))
    parser.add_argument("--api", default=os.environ.get("IPC_API", DEFAULT_API))
    parser.add_argument("--hours", type=float, default=6.0, help="overall budget")
    parser.add_argument("--per-challenge-minutes", type=float, default=45.0)
    parser.add_argument("--poll", type=float, default=20.0)
    parser.add_argument("--max-challenges", type=int, default=0)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="list challenges and exit")
    parser.add_argument(
        "--no-vpn-check",
        action="store_true",
        help="accepted for compatibility; the run preflight reports network state",
    )
    args = parser.parse_args()

    if not args.token or not args.base_url:
        print("BENCHMARK_TOKEN and BENCHMARK_BASE_URL are required", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RunLog(args.log_dir / f"run-{stamp}")
    log.event("run_started", api=args.api, hours=args.hours, dry_run=args.dry_run)

    stop = {"requested": False}

    def request_stop(_signum, _frame):
        stop["requested"] = True
        log.event("stop_requested")

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    deadline = time.time() + args.hours * 3600
    run_id = ""
    exit_code = 0
    try:
        if args.dry_run:
            try:
                from tsec_benchmark import TSecBenchmark, VpnCheckError
            except ImportError:
                print("pip install tsec-benchmark first", file=sys.stderr)
                return 2
            try:
                with TSecBenchmark(
                    base_url=args.base_url,
                    token=args.token,
                    auto_check_vpn=not args.no_vpn_check,
                ) as client:
                    challenges = client.list_challenges()
            except VpnCheckError as exc:
                log.event("vpn_check_failed", error=str(exc))
                return 3
            log.event(
                "challenges_listed",
                total=len(challenges),
                completed=sum(c.is_completed for c in challenges),
            )
            for challenge in challenges:
                print(
                    challenge.unique_code,
                    challenge.difficulty,
                    challenge.correct_flag_count,
                    "/",
                    challenge.flag_count,
                )
            return 0

        workflow_id = create_benchmark_workflow(
            args.api, args.base_url, args.token
        )
        log.event("workflow_ready", workflow_id=workflow_id)
        preflight = api(
            "POST", f"{args.api.rstrip('/')}/api/workflows/{workflow_id}/preflight", None
        )
        log.event(
            "preflight_ok",
            challenge_count=(preflight or {}).get("challenge_count"),
            network=(preflight or {}).get("network"),
        )
        select = None
        if args.max_challenges:
            discovered = [
                str(item.get("external_id"))
                for item in (preflight or {}).get("challenges") or []
                if not item.get("solved")
            ]
            select = discovered[: args.max_challenges]
        run_id = start_run(args.api, workflow_id, select=select)
        log.event("run_started", run_id=run_id, workflow_id=workflow_id, select=select)
        while True:
            snapshot = run_snapshot(args.api, run_id)
            status = record_snapshot(log, snapshot)
            if status in {"finished", "stopped"}:
                log.event("run_terminal", status=status)
                break
            if stop["requested"] or time.time() >= deadline:
                log.event("run_stopping", reason="signal" if stop["requested"] else "deadline")
                stop_run(args.api, run_id)
                exit_code = 0
                break
            time.sleep(max(1.0, args.poll))
    except Exception as exc:  # noqa: BLE001 - the runner reports, it does not crash
        log.event("run_failed", error=f"{type(exc).__name__}: {exc}"[:500])
        exit_code = 1
    finally:
        log.state["finished_at"] = now()
        log.flush()

    log.event(
        "run_finished",
        run_id=run_id,
        submitted=log.state["submitted"],
        awarded=log.state["awarded"],
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
