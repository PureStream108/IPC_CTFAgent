#!/usr/bin/env python3
"""Validate an IPC PlatformWorkflowSpec without accepting credential values."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

from pydantic import ValidationError

from backend.ops.models import PlatformWorkflowSpec

MAX_INPUT_BYTES = 2 * 1024 * 1024


def read_json(source: str) -> dict:
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        from pathlib import Path

        path = Path(source)
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError(f"workflow exceeds {MAX_INPUT_BYTES} byte limit")
        raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"workflow exceeds {MAX_INPUT_BYTES} byte limit")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("workflow must be a JSON object")
    return value


def origin(url: str) -> str:
    parsed = urlparse(url.replace("{{external_id}}", "challenge-id"))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an IPC platform workflow JSON file.")
    parser.add_argument("workflow", help="Workflow JSON path, or '-' for stdin")
    parser.add_argument("--canonical", action="store_true", help="Include canonical validated spec")
    args = parser.parse_args()
    try:
        spec = PlatformWorkflowSpec.model_validate(read_json(args.workflow))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    urls = [spec.challenges.list_url]
    if spec.challenges.attachment_base_url:
        urls.append(spec.challenges.attachment_base_url)
    if spec.submit is not None:
        urls.append(spec.submit.url)
    result = {
        "valid": True,
        "required_secrets": sorted(spec.required_secret_names()),
        "confirmed_origins": sorted({origin(url) for url in urls}),
        "private_network_opt_in": spec.allow_private_networks,
        "has_submit": spec.submit is not None,
    }
    if args.canonical:
        result["workflow"] = spec.model_dump(mode="json")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
