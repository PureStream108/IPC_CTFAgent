from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

TERMINAL = {"finished", "stopped"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--interval", type=float, default=20.0)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    session = requests.Session()
    while True:
        try:
            view = session.get(f"{base}/api/runs/{args.run_id}", timeout=30)
            view.raise_for_status()
            data = view.json()
        except requests.RequestException as exc:
            print(json.dumps({"watchdog": "poll_failed", "error": str(exc)[:200]}), flush=True)
            time.sleep(args.interval)
            continue
        run = data["run"]
        status = run["status"]
        solved = sum(1 for c in data.get("challenges", []) if c.get("state") == "solved")
        print(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "status": status,
            "solved": solved, "target": args.target,
        }), flush=True)
        if status in TERMINAL or status == "paused":
            print(json.dumps({"watchdog": "run no longer active", "status": status}), flush=True)
            return 0
        if solved >= args.target and status == "running":
            for attempt in range(12):
                fresh = session.get(f"{base}/api/runs/{args.run_id}", timeout=30).json()["run"]
                if fresh["status"] != "running":
                    print(json.dumps({"watchdog": "status changed before pause",
                                      "status": fresh["status"]}), flush=True)
                    return 0
                response = session.post(
                    f"{base}/api/runs/{args.run_id}/pause",
                    json={"revision": fresh["revision"]}, timeout=60,
                )
                if response.status_code == 200:
                    print(json.dumps({"watchdog": "paused", "solved": solved,
                                      "status": response.json().get("run", {}).get("status")}), flush=True)
                    return 0
                if response.status_code == 409:
                    time.sleep(int(response.headers.get("Retry-After", "20")))
                    continue
                print(json.dumps({"watchdog": "pause_failed", "status_code": response.status_code,
                                  "detail": response.text[:200]}), flush=True)
                return 1
            print(json.dumps({"watchdog": "pause_failed", "detail": "retries exhausted"}), flush=True)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
