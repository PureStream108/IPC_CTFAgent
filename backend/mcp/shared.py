from __future__ import annotations

import asyncio
import atexit
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from backend.mcp.mcp_server import MCPServer, create_mcp_server

class _TitleAndTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._hidden_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        elif self._hidden_depth == 0:
            self.text_parts.append(text)


def _html_summary(body: str, max_chars: int = 6000) -> tuple[str, str]:
    parser = _TitleAndTextParser()
    parser.feed(body)
    title = " ".join(parser.title_parts).strip()
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return title, text[:max_chars]


def _tool_unavailable(tool: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "tool": tool, "error": detail, **extra}


class _BrowserSession:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()

    async def page(self):
        async with self._lock:
            if self._page is not None:
                return self._page
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
            return self._page

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._playwright = self._browser = self._context = self._page = None

    async def context(self):
        await self.page()
        return self._context


async def _page_snapshot(page) -> dict[str, Any]:
    title = await page.title()
    text = await page.locator("body").inner_text()
    return {"final_url": page.url, "title": title, "text": text[:20000]}


def _close_browser_at_exit(browser: _BrowserSession) -> None:
    if browser._playwright is None:
        return
    try:
        asyncio.run(browser.close())
    except Exception:
        pass


def build_browser_mcp(browser: _BrowserSession | None = None) -> MCPServer:
    server = create_mcp_server("browser", "Stateful Playwright browser tools")
    browser = browser or _BrowserSession()
    atexit.register(_close_browser_at_exit, browser)

    @server.tool(
        name="navigate",
        description="Render a URL and return status, final URL, title, and visible text.",
    )
    async def navigate(
        url: str,
        wait_until: str = "load",
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        try:
            page = await browser.page()
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            snapshot = await _page_snapshot(page)
            content_type = ""
            status = None
            javascript_redirect = False
            if response is not None:
                status = response.status
                content_type = (await response.all_headers()).get("content-type", "")
                javascript_redirect = page.url != url and response.request.redirected_from is None
                if content_type and "html" not in content_type and "text" not in content_type:
                    body = (await response.body()).decode(errors="replace")
                    title, text = _html_summary(body)
                    snapshot.update({"title": title, "text": text or body[:20000]})
            return {
                "available": True,
                "url": url,
                "status": status,
                "content_type": content_type,
                "javascript_redirect": javascript_redirect,
                **snapshot,
            }
        except Exception as exc:
            return _tool_unavailable("browser.navigate", str(exc), url=url)

    @server.tool(name="click", description="Click an element and return the resulting page state.")
    async def click(selector: str) -> dict[str, Any]:
        try:
            page = await browser.page()
            await page.click(selector)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return {"available": True, "selector": selector, **await _page_snapshot(page)}
        except Exception as exc:
            return _tool_unavailable("browser.click", str(exc), selector=selector)

    @server.tool(name="fill", description="Fill an input or textarea without submitting it.")
    async def fill(selector: str, value: str) -> dict[str, Any]:
        try:
            page = await browser.page()
            await page.fill(selector, value)
            return {"available": True, "selector": selector, "value": value, "url": page.url}
        except Exception as exc:
            return _tool_unavailable("browser.fill", str(exc), selector=selector)

    @server.tool(name="press", description="Press a keyboard key on the current page.")
    async def press(key: str) -> dict[str, Any]:
        try:
            page = await browser.page()
            await page.keyboard.press(key)
            return {"available": True, "key": key, **await _page_snapshot(page)}
        except Exception as exc:
            return _tool_unavailable("browser.press", str(exc), key=key)

    @server.tool(name="eval_js", description="Evaluate JavaScript in the current page context.")
    async def eval_js(script: str) -> dict[str, Any]:
        try:
            page = await browser.page()
            return {"available": True, "result": await page.evaluate(script), "url": page.url}
        except Exception as exc:
            return _tool_unavailable("browser.eval_js", str(exc))

    @server.tool(name="get_content", description="Return rendered HTML and visible text for the current page.")
    async def get_content() -> dict[str, Any]:
        try:
            page = await browser.page()
            return {
                "available": True,
                "url": page.url,
                "html": await page.content(),
                "text": await page.locator("body").inner_text(),
            }
        except Exception as exc:
            return _tool_unavailable("browser.get_content", str(exc))

    @server.tool(
        name="screenshot",
        description="Capture the current rendered page with Playwright.",
    )
    async def screenshot(
        path: str | None = None,
        full_page: bool = True,
    ) -> dict[str, Any]:
        out = Path(path) if path else Path.cwd() / f"browser_{int(time.time() * 1000)}.png"
        try:
            page = await browser.page()
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=full_page)
            return {"available": True, "url": page.url, "path": str(out)}
        except Exception as exc:
            return _tool_unavailable("browser.screenshot", str(exc), path=str(out))

    @server.tool(name="cookies", description="Return cookies from the persistent browser context.")
    async def cookies() -> dict[str, Any]:
        try:
            context = await browser.context()
            return {"available": True, "cookies": await context.cookies()}
        except Exception as exc:
            return _tool_unavailable("browser.cookies", str(exc))

    @server.tool(name="set_cookie", description="Add or replace a cookie in the persistent browser context.")
    async def set_cookie(
        name: str,
        value: str,
        url: str | None = None,
        domain: str | None = None,
        path: str = "/",
        http_only: bool = False,
        secure: bool = False,
        same_site: str = "Lax",
    ) -> dict[str, Any]:
        try:
            context = await browser.context()
            cookie: dict[str, Any] = {
                "name": name,
                "value": value,
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": same_site.capitalize(),
            }
            if url:
                cookie["url"] = url
            elif domain:
                cookie["domain"] = domain
                cookie["path"] = path
            else:
                page = await browser.page()
                cookie["url"] = page.url
            await context.add_cookies([cookie])
            return {"available": True, "cookie": cookie}
        except Exception as exc:
            return _tool_unavailable("browser.set_cookie", str(exc), name=name)

    return server


def _zap_base() -> str:
    return os.environ.get("ZAP_API_URL", "http://ipc-zap:8080").rstrip("/")


def _zap_get(path: str, **params: Any) -> dict[str, Any]:
    api_key = os.environ.get("ZAP_API_KEY")
    if api_key:
        params["apikey"] = api_key
    resp = requests.get(f"{_zap_base()}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _spider(url: str) -> dict[str, Any]:
    try:
        scan = _zap_get("/JSON/spider/action/scan/", url=url)
        scan_id = scan.get("scan")
        urls = _zap_get("/JSON/core/view/urls/", baseurl=url).get("urls", [])
    except (requests.RequestException, ValueError) as exc:
        return _tool_unavailable("zap.spider", str(exc), url=url, urls_found=[])
    return {"available": True, "url": url, "scan": scan_id, "urls_found": urls}


def _active_scan(url: str) -> dict[str, Any]:
    try:
        scan = _zap_get("/JSON/ascan/action/scan/", url=url)
        alerts = _zap_get("/JSON/core/view/alerts/", baseurl=url).get("alerts", [])
    except (requests.RequestException, ValueError) as exc:
        return _tool_unavailable("zap.active_scan", str(exc), url=url, alerts=[])
    return {"available": True, "url": url, "scan": scan.get("scan"), "alerts": alerts}


def build_zap_mcp() -> MCPServer:
    server = create_mcp_server("zap", "OWASP ZAP API adapter")

    @server.tool(
        name="spider",
        description="Run ZAP spider against a target URL and return discovered URLs.",
    )
    async def spider(url: str) -> dict[str, Any]:
        return await asyncio.to_thread(_spider, url)

    @server.tool(
        name="active_scan",
        description="Run a ZAP active scan against a target and return current alerts.",
    )
    async def active_scan(url: str) -> dict[str, Any]:
        return await asyncio.to_thread(_active_scan, url)

    return server
