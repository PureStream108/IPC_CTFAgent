"""Open the i春秋 match console and capture the account token.

    python -m backend.platform.ichunqiu.browser_setup --url-key 2026qwbs10

The console only reveals the signed API token after a human login (the login
form needs an image captcha, so it cannot be automated safely).  This module
drives a persistent Chromium/Edge profile, waits for the human to log in, then
saves the token the page itself received from /common/account_token under
data/exports/ichunqiu/credentials.json.

Playwright is imported inside main(): it is an optional e2e dependency and
installing the backend must not require a browser.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

OUT = Path("data/exports/ichunqiu")
PROFILE = OUT / "chrome-profile"
LOG = OUT / "setup.log"
CREDS = OUT / "credentials.json"
DEFAULT_URL_KEY = "2026qwbs10"

_LOCAL_STORAGE_PROBE = """() => {
  const out = {};
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    const value = localStorage.getItem(key) || '';
    if (/token|login|user/i.test(key) || /token/i.test(value.slice(0, 400))) {
      out[key] = value.slice(0, 400);
    }
  }
  return out;
}"""


def note(message: object) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(message)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def _token_from(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    token = str(data.get("token") or "").strip()
    if not token or token.startswith("not_login:"):
        return ""
    return token


def _match_key_from(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("url_key") or "").strip()


def _token_from_body(body: str) -> str:
    """Any signed request body carries the caller's token; parse it read-only."""

    if not body or '"token"' not in body:
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    token = str(payload.get("token") or "").strip()
    if not token or token.startswith("not_login:"):
        return ""
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture an i春秋 account token")
    parser.add_argument("--url-key", default=DEFAULT_URL_KEY, help="match slug, for example 2026qwbs10")
    parser.add_argument("--base-url", default="https://match.ichunqiu.com")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait for login")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--workflow",
        default="",
        help=(
            "install the captured token into this workflow's ichunqiu_token secret"
            " (run from the repository root so ./data/ops-agent is the deployment store)"
        ),
    )
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    url = args.base_url.rstrip("/") + "/" + args.url_key
    note("=== i春秋 token capture start: " + url + " ===")
    state = {"token": "", "match_key": ""}
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE.resolve()),
            channel="msedge",
            headless=args.headless,
            args=["--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response) -> None:
            if "apiterminator.ichunqiu.com" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            found = _token_from(payload)
            if found and not state["token"]:
                state["token"] = found
                note("token captured from response " + response.url.split("/")[-1])
            key = _match_key_from(payload)
            if key:
                state["match_key"] = key

        def on_request(request) -> None:
            if "apiterminator.ichunqiu.com" not in request.url:
                return
            found = _token_from_body(request.post_data or "")
            if found and not state["token"]:
                state["token"] = found
                note("token captured from request " + request.url.split("/")[-1])

        context.on("response", on_response)
        context.on("request", on_request)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        note("browser opened; log in with the account that joins this match")

        deadline = time.time() + max(60, args.timeout)
        reloads = 0
        while time.time() < deadline and not state["token"]:
            page.wait_for_timeout(3000)
            if state["token"]:
                break
            try:
                stored = page.evaluate(_LOCAL_STORAGE_PROBE)
            except Exception:  # noqa: BLE001
                stored = {}
            for key, value in (stored or {}).items():
                text = str(value)
                if "token" in text and "not_login" not in text:
                    marker = '"token":"'
                    if marker in text:
                        candidate = text.split(marker, 1)[1].split('"', 1)[0]
                        if candidate and not candidate.startswith("not_login:"):
                            state["token"] = candidate
                            note("token captured from localStorage key " + str(key))
                            break
            if state["token"]:
                break
            if "/login" not in page.url and reloads < 5:
                reloads += 1
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    note("page reload failed; retrying while the login window remains open")
                page.wait_for_timeout(2000)

        if not state["token"]:
            note("login timeout; no token captured")
            context.close()
            return 2

        note("token captured (len=" + str(len(state["token"])) + ") match_key=" + str(bool(state["match_key"])))
        payload = {
            "url": page.url,
            "base_url": args.base_url.rstrip("/"),
            "url_key": args.url_key,
            "match_key": state["match_key"],
            "token": state["token"],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        CREDS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        note("credentials written to " + str(CREDS))
        if args.workflow:
            # The platform watcher polls with the stored secret, so a live match
            # needs the fresh token installed, not just captured to a file.
            try:
                from backend.ops.store import OpsStore

                store = OpsStore(".")
                try:
                    store.save_workflow_secrets(
                        args.workflow, {"ichunqiu_token": state["token"]}
                    )
                finally:
                    # The pool's threads otherwise outlive the interpreter.
                    store.db.close()
                note("token installed into workflow " + args.workflow)
            except Exception as exc:  # noqa: BLE001 - capture stays useful alone
                note("workflow secret update failed: " + str(exc))
        page.wait_for_timeout(1500)
        context.close()
    note("=== i春秋 token capture end ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
