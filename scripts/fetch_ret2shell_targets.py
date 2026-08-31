from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from backend.platform.ret2shell import Ret2ShellClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = PROJECT_ROOT / "platform_targets" / "ret2shell.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch ret2shell challenges for one game into a YAML target list."
    )
    parser.add_argument("--base-url", default=os.getenv("IPC_R2S_BASE_URL", ""))
    parser.add_argument("--game", type=int, default=None)
    parser.add_argument("--username", default=os.getenv("IPC_R2S_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("IPC_R2S_PASSWORD", ""))
    parser.add_argument("--token", default=os.getenv("IPC_R2S_TOKEN", ""))
    parser.add_argument("--output", type=Path, default=TARGETS_PATH)
    args = parser.parse_args()

    challenges: list[dict] = []
    with Ret2ShellClient(
        base_url=args.base_url,
        game_id=args.game,
        username=args.username,
        password=args.password,
        token=args.token,
    ) as client:
        game = client.get_game()
        for raw in client.list_challenges():
            status = client.challenge_status(raw["id"])
            challenges.append(
                {
                    "id": raw.get("id"),
                    "name": raw.get("name", ""),
                    "category": (raw.get("tag") or [{}])[0].get("name", "misc")
                    if isinstance(raw.get("tag"), list) and raw.get("tag")
                    else "misc",
                    "score": raw.get("score", 0),
                    "solved": bool(status.get("solved")) if isinstance(status, dict) else False,
                    "total_solves": status.get("solves") if isinstance(status, dict) else None,
                }
            )
    challenges.sort(key=lambda item: (str(item["category"]), item["id"] or 0))

    data = {
        "updated_at": datetime.now(UTC).isoformat(),
        "game": {
            "id": game.get("id"),
            "name": game.get("name", ""),
            "start_at": game.get("start_at"),
            "end_at": game.get("end_at"),
        },
        "challenge_count": len(challenges),
        "challenges": challenges,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"wrote {len(challenges)} challenges to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
