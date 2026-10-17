from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
WF = "wf_e99622b0bc7a49ca"
TEAM_SOLVED = {1214, 1218, 1235, 1237, 1238, 1241, 1249, 1267, 1269, 1270}
OUT = Path("data/exports/run_start_info.json")


def main() -> int:
    preflight = requests.post(f"{BASE}/api/workflows/{WF}/preflight", timeout=300)
    preflight.raise_for_status()
    challenges = preflight.json().get("challenges", [])
    selection = [str(c["external_id"]) for c in challenges if int(c["external_id"]) not in TEAM_SOLVED]
    print(json.dumps({"selection": len(selection)}), flush=True)
    digest = hashlib.sha256(",".join(sorted(selection)).encode()).hexdigest()[:32]
    key = f"monitor-{digest}"
    try:
        response = requests.post(
            f"{BASE}/api/workflows/{WF}/start",
            json={"idempotency_key": key, "select": selection},
            timeout=3600,
        )
    except requests.RequestException as exc:
        print(json.dumps({"start_request_error": str(exc)[:200]}), flush=True)
        time.sleep(20)
        runs = requests.get(f"{BASE}/api/runs", timeout=30).json().get("runs", [])
        candidates = [r for r in runs if r.get("idempotency_key") == key]
        if not candidates:
            latest = max(runs, key=lambda r: r.get("created_at", ""), default=None)
            if latest is None:
                raise
            candidates = [latest]
        run = candidates[0]
    else:
        response.raise_for_status()
        run = response.json().get("run", {})
    info = {"run_id": run.get("id"), "status": run.get("status"),
            "idempotency_key": key, "selection": selection,
            "selection_size": len(selection)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": info["run_id"], "status": info["status"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
