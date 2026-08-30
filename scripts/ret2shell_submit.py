from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

API = os.getenv("IPC_API", "http://127.0.0.1:8000")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Submit one flag for a project through the backend platform-verdict "
            "API, so the submission is rate-limited, deduplicated by "
            "(project, flag), and the verdict is applied to local state "
            "(accepted -> completed, rejected -> feedback + reopen). "
            "Dry-run by default; pass --no-dry-run to actually submit."
        )
    )
    parser.add_argument("--session", default=os.getenv("IPC_SESSION", ""),
                        help="admin ipc_session cookie (or IPC_SESSION env)")
    parser.add_argument("--project", required=True, help="project id, e.g. proj_001")
    parser.add_argument("--flag", default="", help="defaults to the project's recorded flag")
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually POST the flag (the shared quota is 10 submissions / 5 minutes).",
    )
    args = parser.parse_args()

    if not args.no_dry_run:
        print(f"dry-run ok: would submit project {args.project} flag {args.flag or '<recorded>'!r}")
        return 0
    if not args.session:
        print("missing --session (or IPC_SESSION env)")
        return 1

    body = json.dumps({"flag": args.flag} if args.flag else {}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/api/flags/{args.project}/submit",
        method="POST",
        data=body,
        headers={
            "Cookie": f"ipc_session={args.session}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"submission failed: HTTP {exc.code} {detail}")
        return 1
    print("verdict:", result)
    return 0 if result.get("solved") else 2


if __name__ == "__main__":
    raise SystemExit(main())
