from __future__ import annotations

import argparse
import os

from backend.platform.ret2shell import Ret2ShellClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ret2shell smoke check: login, game, challenges."
    )
    parser.add_argument("--base-url", default=os.getenv("IPC_R2S_BASE_URL", ""))
    parser.add_argument("--game", type=int, default=None)
    parser.add_argument("--username", default=os.getenv("IPC_R2S_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("IPC_R2S_PASSWORD", ""))
    parser.add_argument("--token", default=os.getenv("IPC_R2S_TOKEN", ""))
    args = parser.parse_args()

    with Ret2ShellClient(
        base_url=args.base_url,
        game_id=args.game,
        username=args.username,
        password=args.password,
        token=args.token,
    ) as client:
        print("ping:", client.ping())
        profile = client.get_profile()
        print("profile:", profile.get("account"), profile.get("nickname"))
        game = client.get_game()
        print(
            "game:",
            game.get("id"),
            game.get("name"),
            "start=", game.get("start_at"),
            "end=", game.get("end_at"),
        )
        challenges = client.list_challenges()
        print("challenge_count:", len(challenges))
        for raw in challenges:
            print(
                raw.get("id"),
                raw.get("name"),
                "score=", raw.get("score"),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
