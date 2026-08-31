from __future__ import annotations

import argparse
import os

from backend.platform.ret2shell import (
    Ret2ShellClient,
    Ret2ShellError,
    Ret2ShellPreflightError,
    Ret2ShellRateLimitError,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Submit one flag to a ret2shell challenge. Dry-run by default so a "
            "mistyped command never spends a platform attempt; pass "
            "--no-dry-run to actually submit."
        )
    )
    parser.add_argument("--base-url", default=os.getenv("IPC_R2S_BASE_URL", ""))
    parser.add_argument("--game", type=int, default=None)
    parser.add_argument("--username", default=os.getenv("IPC_R2S_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("IPC_R2S_PASSWORD", ""))
    parser.add_argument("--token", default=os.getenv("IPC_R2S_TOKEN", ""))
    parser.add_argument("--challenge", type=int, required=True)
    parser.add_argument("--flag", required=True)
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually POST the flag (the shared quota is 10 submissions / 5 minutes).",
    )
    args = parser.parse_args()

    with Ret2ShellClient(
        base_url=args.base_url,
        game_id=args.game,
        username=args.username,
        password=args.password,
        token=args.token,
    ) as client:
        status = client.challenge_status(args.challenge)
        print("challenge status before submit:", status)
        if isinstance(status, dict) and status.get("solved"):
            print("already solved; nothing to submit")
            return 0
        if not args.no_dry_run:
            print(f"dry-run ok: would submit {args.flag!r} to challenge {args.challenge}")
            return 0
        try:
            submission = client.submit_flag(
                args.challenge, args.flag, check_solved=False
            )
        except Ret2ShellPreflightError as exc:
            print(f"preflight refused: {exc}")
            return 1
        except Ret2ShellRateLimitError as exc:
            print(f"rate limited: {exc}")
            return 1
        except Ret2ShellError as exc:
            print(f"submission failed: {exc}")
            return 1
        print(
            "submission",
            submission.get("id"),
            "solved=", submission.get("solved"),
            "result=", submission.get("result"),
        )
        return 0 if submission.get("solved") else 2


if __name__ == "__main__":
    raise SystemExit(main())
