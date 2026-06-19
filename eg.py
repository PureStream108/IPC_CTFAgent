from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import requests
import yaml


DEFAULT_CONFIG = Path("backend/config/config.yaml")


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if len(value) <= 8:
        return "***" if value else ""
    return f"{value[:4]}***{value[-4:]}"


def load_targets(config_path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    targets: list[dict[str, Any]] = []

    diamond = raw.get("diamond") or {}
    targets.append(
        {
            "name": "diamond",
            "api_key": (diamond.get("api_key") or "").strip(),
            "base_url": (diamond.get("base_url") or "").strip(),
            "model": (diamond.get("model") or "gpt-4o").strip(),
        }
    )

    for member in raw.get("members") or []:
        targets.append(
            {
                "name": f"member:{(member.get('name') or 'unknown').strip()}",
                "api_key": (member.get("api_key") or "").strip(),
                "base_url": (member.get("base_url") or "").strip(),
                "model": (member.get("model") or "gpt-4o").strip(),
            }
        )

    return targets


def test_target(target: dict[str, Any], timeout: int, model_override: str | None) -> int:
    name = target["name"]
    api_key = target["api_key"]
    base_url = target["base_url"].rstrip("/")
    model = (model_override or target["model"] or "gpt-4o").strip()
    endpoint = f"{base_url}/chat/completions"

    print(f"[{name}]")
    print(f"  endpoint: {endpoint}")
    print(f"  model:    {model}")
    print(f"  api_key:  {mask_secret(api_key)}")

    if not api_key or not base_url:
        print("  result:   skipped (missing api_key or base_url)")
        print()
        return 2

    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 8,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print(f"  result:   request error: {exc}")
        print()
        return 3

    print(f"  status:   {resp.status_code}")

    body_preview = resp.text.strip().replace("\r", " ").replace("\n", " ")
    if len(body_preview) > 300:
        body_preview = body_preview[:300] + "..."
    print(f"  body:     {body_preview}")

    ok = 200 <= resp.status_code < 300
    print(f"  result:   {'OK' if ok else 'FAIL'}")
    print()
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Quick API key tester for OpenAI-compatible endpoints.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.yaml")
    parser.add_argument("--only", help="Only test one target name, e.g. diamond or member:aventurine")
    parser.add_argument("--model", help="Override model for all requests")
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 4

    targets = load_targets(args.config)
    if args.only:
        targets = [target for target in targets if target["name"] == args.only]
        if not targets:
            print(f"Target not found: {args.only}", file=sys.stderr)
            return 5

    codes = [test_target(target, args.timeout, args.model) for target in targets]
    return max(codes) if codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
