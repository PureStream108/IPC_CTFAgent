from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from backend.platform.gzctf import GZCTFClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = PROJECT_ROOT / "platform_targets" / "gzctf.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch visible GZCTF challenges for one game into a YAML target list."
    )
    parser.add_argument("--base-url", default=os.getenv("IPC_GZ_BASE_URL", ""))
    parser.add_argument("--username", default=os.getenv("IPC_GZ_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("IPC_GZ_PASSWORD", ""))
    parser.add_argument("--game", type=int, required=True)
    parser.add_argument("--output", type=Path, default=TARGETS_PATH)
    args = parser.parse_args()

    challenges: list[dict] = []
    with GZCTFClient(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
    ) as client:
        client.login()
        game = client.get_game(args.game)
        scoreboard = client.get_scoreboard(args.game)
        for category, items in (scoreboard.get("challenges") or {}).items():
            for item in items:
                challenges.append(
                    {
                        "id": item["id"],
                        "title": item.get("title", ""),
                        "category": category,
                        "score": item.get("score", 0),
                        "solved": item.get("solved", 0),
                    }
                )
    challenges.sort(key=lambda item: (item["category"], item["id"]))

    data = {
        "updated_at": datetime.now(UTC).isoformat(),
        "game": {
            "id": game.get("id"),
            "title": game.get("title"),
            "team_name": game.get("teamName"),
            "status": game.get("status"),
            "start": game.get("start"),
            "end": game.get("end"),
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
