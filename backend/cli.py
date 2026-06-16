from __future__ import annotations

import argparse
import sys

from backend.core.config import load_config


def _serve(args) -> int:
    import uvicorn

    uvicorn.run("backend.server.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _health(args) -> int:
    from backend.members.adapters import health_check

    cfg = load_config()
    print("Diamond:", health_check(cfg.diamond))
    for m in cfg.members:
        print(f"{m.name}:", health_check(m))
    return 0


def _check(args) -> int:
    cfg = load_config()
    errors = cfg.startup_errors()
    if errors:
        print("NOT READY:")
        for e in errors:
            print("  -", e)
        return 1
    avail = [m.name for m in cfg.available_members()]
    print(f"READY. Diamond configured. Members with creds: {avail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ipc", description="IPC_CTFAgent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the API + UI server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=_serve)

    h = sub.add_parser("health", help="health-check all configured LLM endpoints")
    h.set_defaults(func=_health)

    c = sub.add_parser("check", help="report startup readiness")
    c.set_defaults(func=_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
