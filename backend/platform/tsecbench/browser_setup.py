"""Open the TSecBench console in a real browser and capture its access config.

    python -m backend.platform.tsecbench.browser_setup

The platform only hands out BENCHMARK_TOKEN / BENCHMARK_BASE_URL through the
logged-in console, and that console needs an interactive Tencent login.  This
module drives a persistent Chrome profile, waits for a human login, then saves
the downloaded ovpn file and the captured credentials under
data/exports/tsecbench.

Playwright is imported inside main(): it is an optional e2e dependency, and
installing the backend must not require a browser.
"""

import json
import re
import time
from pathlib import Path

URL = "https://tsecbench.zc.tencent.com/benchmark/prepare/4?mode=local&evaluation_type=agent"
OUT = Path("data/exports/tsecbench")
PROFILE = OUT / "chrome-profile"
LOG = OUT / "setup.log"
CREDS = OUT / "credentials.json"


def note(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def snapshot(page, name):
    try:
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        (OUT / f"{name}.txt").write_text(page.inner_text("body"), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        note(f"snapshot {name} failed: {exc}")


def parse_credentials(text):
    token = None
    base = None
    patterns = [
        (r"BENCHMARK_TOKEN\s*[:=]\s*([A-Za-z0-9\-]{8,})", "token"),
        (r"TOKEN\s*[:=]\s*([A-Za-z0-9\-]{8,})", "token"),
        (r"BENCHMARK_BASE_URL\s*[:=]\s*(https?://\S+)", "base"),
        (r"BASE_URL\s*[:=]\s*(https?://\S+)", "base"),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, text)
        if match:
            if kind == "token" and not token:
                token = match.group(1)
            if kind == "base" and not base:
                base = match.group(1).rstrip(".,);]")
    return token, base



def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    note("=== setup start ===")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE.resolve()), channel="chrome", headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1440, "height": 1100}, accept_downloads=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        downloads = []
        captures = []

        def on_download(download):
            target = OUT / (download.suggested_filename or "benchmark.ovpn")
            try:
                download.save_as(str(target))
                downloads.append(str(target))
                note(f"download saved: {target}")
            except Exception as exc:  # noqa: BLE001
                note(f"download failed: {exc}")

        ctx.on("download", on_download)

        def on_response(resp):
            if "/api/" in resp.url and resp.request.method in ("POST", "PUT", "PATCH"):
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    body = ""
                captures.append({"status": resp.status, "url": resp.url, "body": body[:6000]})

        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        note("page loaded")

        if page.locator("button", has_text="登录 / 注册").count():
            note("not logged in: waiting up to 60 minutes for a human login")
            deadline = time.time() + 3600
            while time.time() < deadline:
                if page.locator("button", has_text="登录 / 注册").count() == 0:
                    note("login detected")
                    break
                for other in ctx.pages:
                    if other is not page and "zc.tencent.com" in other.url:
                        pass
                page.wait_for_timeout(5000)
            else:
                note("login timeout")
                snapshot(page, "setup_login_timeout")
                ctx.close()
                raise SystemExit(2)
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
            except Exception as exc:  # noqa: BLE001
                note(f"reload after login failed: {exc}")
        else:
            note("already logged in")

        snapshot(page, "setup_after_login")

        if page.locator("text=新增 Agent").count() == 0:
            add = page.locator("button", has_text="新增").first
            if add.count():
                add.click()
                page.wait_for_timeout(1500)
        name_box = page.locator("input[placeholder*='My-Pentest-Agent']").first
        if name_box.count():
            name_box.fill("IPC-CTFAgent")
        intro = page.locator("textarea").first
        if intro.count() and not intro.input_value():
            intro.fill("Multi-agent pentest solver: Diamond scheduler, 10 Member seats, PostgreSQL blackboard.")
        save = page.get_by_role("button", name="保存")
        if save.count():
            save.click()
            page.wait_for_timeout(4000)
        note("agent step done")
        snapshot(page, "setup_after_save")

        if "身份验证" in page.inner_text("body"):
            note("still gated after save")
            ctx.close()
            raise SystemExit(3)

        trigger = page.locator("button", has_text="请选择 Agent").first
        if trigger.count():
            trigger.click()
            page.wait_for_timeout(1200)
            try:
                page.get_by_text("IPC-CTFAgent", exact=True).first.click()
            except Exception as exc:  # noqa: BLE001
                note(f"select agent failed: {exc}")
            page.wait_for_timeout(1500)
        snapshot(page, "setup_agent_selected")

        box = page.locator("input[type=checkbox]").first
        if box.count() and not box.is_checked():
            box.check()
        page.wait_for_timeout(500)

        page.locator("button", has_text="获取接入配置").first.click()
        page.wait_for_timeout(15000)
        snapshot(page, "setup_access_config")
        text = ""
        for pg in ctx.pages:
            try:
                text += "\n" + pg.inner_text("body")
            except Exception:  # noqa: BLE001, S110
                pass
        token, base = parse_credentials(text)
        for item in captures:
            if not token or not base:
                t2, b2 = parse_credentials(item["body"])
                token = token or t2
                base = base or b2
        payload = {
            "url": page.url,
            "token": token,
            "base_url": base or "https://tsecbench.zc.tencent.com",
            "downloads": downloads,
            "captures": captures,
            "text_tail": text[-4000:],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        CREDS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        note(f"credentials written token={bool(token)} base={payload['base_url']} downloads={downloads}")
        page.wait_for_timeout(5000)
        ctx.close()
    note("=== setup end ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
