from __future__ import annotations

import argparse
import os

from backend.platform.gzctf import GZCTFClient

DEFAULT_BASE_URL = ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check GZCTF login, profile and game state."
    )
    parser.add_argument("--base-url", default=os.getenv("IPC_GZ_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.getenv("IPC_GZ_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("IPC_GZ_PASSWORD", ""))
    parser.add_argument("--game", type=int, default=2)
    args = parser.parse_args()

    with GZCTFClient(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
    ) as client:
        client.login()
        profile = client.get_profile()
        print("profile:", profile.get("userName"), profile.get("email"))

        game = client.get_game(args.game)
        print(
            "game:",
            game.get("id"),
            game.get("title"),
            "status=", game.get("status"),
            "team=", game.get("teamName"),
        )

        check = client.get_game_check(args.game)
        print("check:", check)

        scoreboard = client.get_scoreboard(args.game)
        challenges = scoreboard.get("challenges", {})
        challenge_count = sum(len(items) for items in challenges.values())
        print("scoreboard challenge_count:", challenge_count)
        for category, items in challenges.items():
            print(category, [(item.get("id"), item.get("title")) for item in items][:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
