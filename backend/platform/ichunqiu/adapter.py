"""Native adapter for the i春秋 match platform (match.ichunqiu.com).

The competition site is a single-page app whose JSON API lives on
``apiterminator.ichunqiu.com``.  Every call is a POST that carries four signed
fields in addition to the endpoint payload:

    k      opaque match key resolved from the public slug (2026qwbs10)
    stamp  client time in milliseconds
    token  account token captured from a logged-in browser session
    rs     per-request random nonce

and a ``SIGN`` header equal to ``sha1(sorted "k=v" pairs + salt)``.  The browser
only learns the account token after a human login, so this adapter never
guesses credentials: it consumes the token written by
``backend.platform.ichunqiu.browser_setup``.

Endpoints used here are the ones the participant console calls:

    /match/detail              public match metadata (title, times, quotas)
    /match/question/class      categories with their visible challenges
    /match/question/lists      per-category challenge detail (needs login)
    /match/flag/submit         flag submission
    /match/docker/info|apply|process|delay|close   per-team containers
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests

from backend.platform._common import PLATFORM_CATEGORIES, numbered_filename, safe_stem
from backend.platform.adapter import PlatformAdapter
from backend.platform.errors import PlatformError, PlatformPreflightError
from backend.platform.mapping import FieldMapping, PlatformChallenge

_logger = logging.getLogger(__name__)

API_ORIGIN = "https://apiterminator.ichunqiu.com"
SIGN_SALT = "7637b08bdb0b29e08300a976b24ca672"
DEFAULT_BASE_URL = "https://match.ichunqiu.com"

#: Categories the platform labels differently from IPC's vocabulary.
_CATEGORY_ALIASES = {
    "misc": "misc",
    "miscellaneous": "misc",
    "crypto": "crypto",
    "cryptography": "crypto",
    "reverse": "reverse",
    "pwn": "pwn",
    "web": "web",
    "ai": "ai",
    "osint": "osint",
    "pentest": "pentest",
}


class IchunqiuError(PlatformError):
    """An i春秋 API call failed."""


class IchunqiuLoginError(IchunqiuError):
    reason = "auth_required"


class IchunqiuPreflightError(PlatformPreflightError):
    pass


def _js_value(value: Any) -> str:
    """Stringify a JSON value the way the browser's template literal does."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sign_payload(payload: dict[str, Any]) -> str:
    pairs = sorted(f"{key}={_js_value(value)}" for key, value in payload.items())
    return hashlib.sha1(("&".join(pairs) + "&" + SIGN_SALT).encode("utf-8")).hexdigest()


def _nonce() -> str:
    return hashlib.md5(f"{time.time()}{random.random()}icq".encode()).hexdigest()


class IchunqiuClient:
    """Token-authenticated participant client for one i春秋 match."""

    def __init__(
        self,
        base_url: str = "",
        url_key: str = "",
        token: str = "",
        *,
        api_origin: str = "",
        timeout: float = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("IPC_ICQ_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
        self.api_origin = (api_origin or os.getenv("IPC_ICQ_API_ORIGIN", "") or API_ORIGIN).rstrip("/")
        self.slug = (url_key or "").strip()
        self.token = (token or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.match: dict[str, Any] = {}
        self.key = ""
        self.logged_in = False

    # -- wire -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Lang": "zh-CN",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        headers["SIGN"] = sign_payload(body)
        try:
            response = self.session.post(
                self.api_origin + path, json=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise IchunqiuError(f"i春秋 request failed: {exc}") from exc
        if response.status_code >= 400:
            message = f"i春秋 HTTP {response.status_code} for {path}"
            if response.status_code in (401, 403):
                self.logged_in = False
                raise IchunqiuLoginError(message)
            error = IchunqiuError(message)
            error.status_code = response.status_code
            retry_after = response.headers.get("Retry-After", "") if hasattr(response, "headers") else ""
            try:
                error.retry_after = max(1, int(retry_after))
            except (TypeError, ValueError):
                error.retry_after = 30
            raise error
        try:
            envelope = response.json()
        except ValueError as exc:
            raise IchunqiuError(f"i春秋 {path} did not return JSON") from exc
        if not isinstance(envelope, dict):
            raise IchunqiuError(f"i春秋 {path} returned an unexpected envelope")
        return envelope

    def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        require_login: bool = True,
    ) -> Any:
        # Every signed call carries the opaque match key; a fresh adapter (for
        # example the one built to start a container) has not resolved it yet,
        # and sending an empty key is answered with "暂无比赛信息".
        if key is None and not self.key:
            self.resolve_match()
        body: dict[str, Any] = dict(payload or {})
        body.setdefault("k", self.key if key is None else key)
        body["stamp"] = int(time.time() * 1000)
        body["token"] = self.token
        body["rs"] = _nonce()
        envelope = self._post(path, body)
        message = str(envelope.get("message") or envelope.get("inform") or "").strip()
        if envelope.get("code") not in (0, None) and "暂无比赛信息" in message and require_login:
            # The match key is per session and goes stale; resolve it again once.
            self.resolve_match()
            body["k"] = self.key
            body["stamp"] = int(time.time() * 1000)
            body["rs"] = _nonce()
            envelope = self._post(path, body)
            message = str(envelope.get("message") or envelope.get("inform") or "").strip()
        code = envelope.get("code")
        if code == 0:
            return envelope.get("data")
        if code in (100, 112, 114, 130) or "登录" in message:
            self.logged_in = False
            raise IchunqiuLoginError(message or "i春秋 account token is missing or expired")
        raise IchunqiuError(f"i春秋 {path} failed: {message or code}")

    # -- discovery ------------------------------------------------------

    def resolve_match(self) -> dict[str, Any]:
        """Resolve the public slug into the opaque key used by every other call."""

        if not self.slug:
            raise IchunqiuPreflightError("i春秋 url_key is not configured")
        data = self.request(
            "/match/detail",
            {"url_key": self.slug, "is_encrypt": 2},
            key="",
            require_login=False,
        )
        if not isinstance(data, dict) or not data.get("url_key"):
            raise IchunqiuError("i春秋 match detail did not contain a match key")
        self.match = data
        self.key = str(data["url_key"])
        return data

    def account(self) -> dict[str, Any]:
        data = self.request("/common/account_token", {}, require_login=False)
        return data if isinstance(data, dict) else {}

    def categories(self) -> list[dict[str, Any]]:
        data = self.request("/match/question/class", {"is_hidden": 0, "show_num": ""})
        return [item for item in data or [] if isinstance(item, dict)]

    def question_lists(self, category_id: Any = None, *, page_size: int = 100) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "child_id": 0,
            "type_ids": "",
            "is_brief": "",
            "page_index": 1,
            "page_size": page_size,
        }
        if category_id is not None:
            payload["category_id"] = category_id
        data = self.request("/match/question/lists", payload)
        if isinstance(data, dict):
            items = data.get("lists") or data.get("list") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    def open_record(self, question_id: str) -> Any:
        return self.request("/match/question/open_record", {"question_id": question_id})

    # -- containers -----------------------------------------------------

    def docker_info(self, question_id: str) -> dict[str, Any]:
        data = self.request("/match/docker/info", {"question_id": question_id})
        return data if isinstance(data, dict) else {}

    def docker_apply(self, question_id: str) -> dict[str, Any]:
        data = self.request("/match/docker/apply", {"question_id": question_id})
        return data if isinstance(data, dict) else {}

    def docker_process(self, uuid: str) -> dict[str, Any]:
        data = self.request("/match/docker/process", {"uuid": uuid})
        return data if isinstance(data, dict) else {}

    def docker_delay(self, uuid: str) -> Any:
        return self.request("/match/docker/delay", {"uuid": uuid})

    def docker_close(self, uuid: str, close_type: int = 1) -> Any:
        return self.request("/match/docker/close", {"uuid": uuid, "close_type": close_type})

    # -- flags ----------------------------------------------------------

    def submit_flag(self, question_id: str, flag: str) -> dict[str, Any]:
        """Submit exactly one flag; the platform itself answers code 0 on success."""

        self.request(
            "/match/flag/submit",
            {"question_id": question_id, "answer_str": flag},
        )
        return {"accepted": True}

    def submit_history(self, question_id: str) -> Any:
        return self.request(
            "/match/flag/submit_lists",
            {"question_id": question_id, "page_index": 1, "page_size": 20},
        )


class IchunqiuAdapter(PlatformAdapter):
    """Map the i春秋 console onto IPC's PlatformAdapter contract."""

    def __init__(
        self,
        client: IchunqiuClient,
        mapping: FieldMapping,
        *,
        max_attachment_bytes: int | None = None,
    ) -> None:
        self.client = client
        self.mapping = mapping
        self.max_attachment_bytes = max_attachment_bytes
        # A match can be worked from attachments alone; in that mode the run
        # never imports a challenge that needs a team container, so it cannot
        # occupy (or evict) one of the team's limited slots.
        self.attachments_only = bool(getattr(mapping, "attachments_only", False))
        # Challenges the operator deliberately keeps out of the run (for example a
        # task whose download is too large to be useful) match by id or title.
        self.exclude = {
            str(value or "").strip().casefold()
            for value in (getattr(mapping, "exclude", None) or [])
            if str(value or "").strip()
        }
        self._by_id: dict[str, PlatformChallenge] = {}
        self._docker: dict[str, dict[str, Any]] = {}

    @property
    def identity(self) -> str:
        return f"{self.client.base_url}/match/{self.client.slug}" if self.client.slug else self.client.base_url

    # -- challenges -----------------------------------------------------

    def _category(self, title: str, external_id: str) -> str:
        mapped = (
            self.mapping.category_map.get(external_id)
            or self.mapping.category_map.get(title)
            or self.mapping.category_map.get("*")
        )
        if mapped:
            return mapped if mapped in PLATFORM_CATEGORIES else "misc"
        key = str(title or "").strip().lower()
        for alias, category in _CATEGORY_ALIASES.items():
            if alias and alias in key:
                return category
        return "misc"

    _ANCHOR = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    _TAG = re.compile(r"<[^>]+>")

    @classmethod
    def _html_to_text(cls, raw: str) -> str:
        """Keep attachment links readable while dropping the rest of the markup."""

        def anchor(match: re.Match[str]) -> str:
            url = match.group(1)
            label = cls._TAG.sub("", match.group(2)).strip() or "附件"
            return f"{label}: {url}"

        text = cls._ANCHOR.sub(anchor, raw)
        text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.IGNORECASE)
        text = cls._TAG.sub("", text)
        return html.unescape(text).strip()

    def _details_of(self, item: dict[str, Any]) -> tuple[str, list[str]]:
        raw = str(item.get("content") or item.get("description") or item.get("detail") or "")
        description = self._html_to_text(raw) if raw else ""
        urls: list[str] = []
        for key in ("file_url", "flag_url", "file", "url", "download_url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
        raw_files = item.get("file_list") or item.get("files") or item.get("attachments") or []
        if isinstance(raw_files, dict):
            raw_files = [raw_files]
        if isinstance(raw_files, list):
            for entry in raw_files:
                if isinstance(entry, str):
                    urls.append(entry)
                elif isinstance(entry, dict):
                    value = entry.get("url") or entry.get("download_url") or entry.get("href")
                    if isinstance(value, str) and value:
                        urls.append(value)
        for match in self._ANCHOR.finditer(raw):
            label = self._TAG.sub("", match.group(2)).strip()
            href = match.group(1).strip()
            if href.startswith(("http://", "https://")) and (
                "附件" in label or "下载" in label or "pan.baidu.com" in href
            ):
                urls.append(href)
        seen: set[str] = set()
        ordered: list[str] = []
        for url in urls:
            absolute = urljoin(self.client.base_url + "/", url)
            if absolute not in seen:
                seen.add(absolute)
                ordered.append(absolute)
        return description, ordered

    def _items(self) -> list[dict[str, Any]]:
        """Return every visible challenge; the detail endpoint already has it all.

        The question list endpoint answers with description, attachment links and
        container metadata in one call, so the lighter question class listing is
        only a fallback for older matches.
        """

        try:
            items = self.client.question_lists(None)
            if items:
                return items
        except IchunqiuLoginError:
            raise
        except IchunqiuError as exc:
            _logger.debug("i春秋 question list failed: %s", exc)
        fallback: list[dict[str, Any]] = []
        for category in self.client.categories():
            listing = category.get("question") or {}
            for item in (listing.get("lists") if isinstance(listing, dict) else []) or []:
                if isinstance(item, dict):
                    fallback.append({**item, "category": item.get("category") or category.get("title")})
        return fallback

    def fetch_challenges(self) -> list[PlatformChallenge]:
        if not self.client.key:
            self.client.resolve_match()
        challenges: list[PlatformChallenge] = []
        for item in self._items():
            external = str(item.get("id") or "")
            title = str(item.get("title") or "")
            if not external or not title:
                continue
            if self.exclude and (
                external.casefold() in self.exclude or title.strip().casefold() in self.exclude
            ):
                continue
            description, attachments = self._details_of(item)
            red_solved = item.get("red_solved")
            solved = bool(red_solved.get("is_solved")) if isinstance(red_solved, dict) else bool(item.get("is_solved"))
            remote = bool(item.get("image_url")) or str(item.get("type_id") or "") in {"2", "5"}
            if self.attachments_only and remote:
                continue
            solves = 0
            if isinstance(red_solved, dict):
                try:
                    solves = int(red_solved.get("solved_number") or 0)
                except (TypeError, ValueError):
                    solves = 0
            challenges.append(
                PlatformChallenge(
                    external_id=external,
                    title=title,
                    category=self._category(str(item.get("category") or ""), external),
                    description=description,
                    attachment_urls=attachments,
                    remote=remote,
                    solved=solved,
                    platform_data={
                        "category_id": item.get("category_id"),
                        "type_id": item.get("type_id"),
                        "score": item.get("red_score"),
                        "image": item.get("image_url"),
                        "solve_count": solves,
                    },
                )
            )
        # The most-solved challenge is the softest target, so it is imported and
        # planned first; the scheduler keeps this order inside its own policy.
        challenges.sort(key=lambda challenge: -int(challenge.platform_data.get("solve_count") or 0))
        self._by_id = {challenge.external_id: challenge for challenge in challenges}
        return challenges

    def preflight(self) -> list[PlatformChallenge]:
        if not self.client.token or self.client.token.startswith("not_login:"):
            raise IchunqiuLoginError(
                "i春秋 account token is missing; run backend.platform.ichunqiu.browser_setup"
            )
        challenges = self.fetch_challenges()
        if not challenges:
            raise IchunqiuPreflightError("i春秋 match exposed no visible challenges")
        self.client.logged_in = True
        return challenges

    def challenges(self) -> list[PlatformChallenge]:
        return self.fetch_challenges()

    def network_diagnostics(self) -> dict[str, Any]:
        return {
            "base_url": self.client.base_url,
            "api_origin": self.client.api_origin,
            "match_key_resolved": bool(self.client.key),
            "logged_in": bool(self.client.token and not self.client.token.startswith("not_login:")),
        }

    # -- attachments ----------------------------------------------------

    def download_attachments(
        self, challenge: PlatformChallenge, dest_dir: str | Path
    ) -> list[Path]:
        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for index, url in enumerate(challenge.attachment_urls, start=1):
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError(f"i春秋 attachment URL must use http or https: {url}")
            source = Path(unquote(parsed.path)).name or f"attachment-{index}"
            target = destination / numbered_filename(
                safe_stem(Path(source).stem, fallback=f"attachment-{index}"),
                Path(source).suffix[:20] or ".bin",
                [path.name for path in destination.iterdir()],
                fallback=f"attachment-{index}",
            )
            response = self.client.session.get(
                url,
                headers={"Referer": self.client.base_url + "/"},
                timeout=self.client.timeout,
                stream=True,
            )
            response.raise_for_status()
            written = 0
            try:
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if self.max_attachment_bytes is not None and written > self.max_attachment_bytes:
                            raise ValueError(
                                f"attachment exceeds {self.max_attachment_bytes} byte limit: {url}"
                            )
                        handle.write(chunk)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            downloaded.append(target)
        return downloaded

    # -- submit ---------------------------------------------------------

    @property
    def supports_submit(self) -> bool:
        return True

    def submit(self, external_id: str, flag: str) -> dict[str, Any]:
        value = str(flag or "").strip()
        if not value:
            return {"verdict": "rejected", "reason": "empty flag"}
        try:
            self.client.submit_flag(external_id, value)
        except IchunqiuLoginError:
            return {"verdict": "auth_required"}
        except IchunqiuError as exc:
            message = str(exc)
            lowered = message.casefold()
            if (
                getattr(exc, "status_code", None) == 429
                or "频繁" in message
                or "too many" in lowered
                or "rate limit" in lowered
            ):
                return {
                    "verdict": "rate_limited",
                    "retry_after": getattr(exc, "retry_after", 30),
                    "message": message,
                }
            if any(marker in message for marker in ("比赛结束", "提交已关闭", "竞赛结束")) or "closed" in lowered:
                return {"verdict": "rejected", "reason": "competition_closed", "message": message}
            if any(marker in message for marker in ("答案错误", "答案不正确", "flag错误", "flag 错误")) or any(
                marker in lowered for marker in ("wrong answer", "incorrect flag", "invalid flag")
            ):
                return {"verdict": "wrong", "message": message}
            return {"verdict": "unknown", "message": message}
        return {"verdict": "correct"}

    # -- instances ------------------------------------------------------

    @property
    def supports_instances(self) -> bool:
        # Attachment-only runs must never open a container: the team quota is
        # small (i春秋 evicts an existing instance when it is exceeded).
        return not self.attachments_only

    def instance_limit(self) -> int | None:
        if self.attachments_only:
            return None
        if not self.client.match:
            self.client.resolve_match()
        for key in ("team_docker_limit", "user_docker_limit"):
            try:
                value = int(self.client.match.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _entry_of(info: dict[str, Any]) -> str:
        """Normalise the platform's connection string into host:port.

        i春秋 answers with a shell-style hint such as "nc 39.106.53.101 34846"
        for a plain TCP service, or with a full URL for a web service.
        """

        raw = str(info.get("url") or "").strip()
        if not raw:
            return ""
        parts = raw.split()
        if len(parts) == 3 and parts[0].casefold() in {"nc", "ncat", "socat"}:
            return f"{parts[1]}:{parts[2]}"
        if len(parts) == 2:
            return f"{parts[0]}:{parts[1]}"
        if raw.startswith(("http://", "https://", "tcp://")):
            return raw
        if ":" in raw and " " not in raw:
            return "http://" + raw
        return raw

    def _view(self, info: dict[str, Any]) -> dict[str, Any]:
        entry = self._entry_of(info)
        ssh_raw = info.get("ssh")
        if isinstance(ssh_raw, list):
            ssh_raw = ssh_raw[0] if ssh_raw and isinstance(ssh_raw[0], dict) else {}
        ssh = ssh_raw if isinstance(ssh_raw, dict) else {}
        view: dict[str, Any] = {
            "state": "ready" if entry else "pending",
            "entry": entry,
            "url": entry,
            "uuid": info.get("uuid"),
        }
        for source, target in (("countdown", "expires_in"), ("delay_count", "renewals_left")):
            if info.get(source) is not None:
                view[target] = info[source]
        if ssh:
            view["ssh"] = ssh
            view["username"] = ssh.get("username")
            view["password"] = ssh.get("password")
        return view

    def instances(self) -> list[dict[str, Any]]:
        if not self._by_id:
            self.fetch_challenges()
        found: list[dict[str, Any]] = []
        for challenge in list(self._by_id.values()):
            if not challenge.remote:
                continue
            try:
                info = self.client.docker_info(challenge.external_id)
            except IchunqiuError:
                continue
            if not isinstance(info, dict) or not (info.get("uuid") or info.get("url")):
                continue
            view = self._view(info)
            found.append({"external_id": challenge.external_id, **view})
        return found

    def _wait_ready(
        self, external_id: str, *, attempts: int = 12, delay: float = 5.0
    ) -> dict[str, Any]:
        """Poll the container info until the platform publishes an address.

        A cold container needs roughly a minute, and the scheduler repairs an
        instance it saw fail, so a short bounded wait is cheaper than holding
        the tick for the whole provisioning window.
        """

        for index in range(attempts):
            info = self._safe_info(external_id)
            if self._entry_of(info):
                return info
            if index + 1 < attempts:
                time.sleep(delay)
        return {}

    def _safe_info(self, external_id: str) -> dict[str, Any]:
        """A not-yet-created container answers with an error, not an empty object."""

        try:
            info = self.client.docker_info(external_id)
        except IchunqiuLoginError:
            raise
        except IchunqiuError:
            return {}
        return info if isinstance(info, dict) else {}

    def start_instance(self, external_id: str) -> dict[str, Any]:
        if not self.supports_instances:
            raise IchunqiuError(
                "i春秋 workflow is attachment-only; container challenges are not imported"
            )
        info = self._safe_info(external_id)
        if self._entry_of(info):
            return self._view(info)
        try:
            self.client.docker_apply(external_id)
        except IchunqiuError as exc:
            # An earlier attempt (or another tab) may already hold the
            # container; the info poll below is the source of truth.
            _logger.debug("i春秋 docker apply for %s: %s", external_id, exc)
        ready = self._wait_ready(external_id)
        view = self._view(ready)
        if not view["entry"]:
            raise IchunqiuError("i春秋 container is still provisioning")
        return view

    def stop_instance(self, external_id: str) -> None:
        info = self._safe_info(external_id)
        uuid = str(info.get("uuid") or "")
        if uuid:
            self.client.docker_close(uuid, 1)

    def renew_instance(self, external_id: str) -> dict[str, Any]:
        info = self._safe_info(external_id)
        uuid = str(info.get("uuid") or "")
        if not uuid:
            raise IchunqiuError("i春秋 container does not exist")
        self.client.docker_delay(uuid)
        refreshed = self._safe_info(external_id)
        return self._view(refreshed or info)


__all__ = [
    "IchunqiuAdapter",
    "IchunqiuClient",
    "IchunqiuError",
    "IchunqiuLoginError",
    "IchunqiuPreflightError",
    "sign_payload",
]
