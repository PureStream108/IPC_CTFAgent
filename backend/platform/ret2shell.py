from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from backend.core.config import CATEGORIES
from backend.filename_util import numbered_filename, safe_stem
from backend.platform.adapter import PlatformAdapter
from backend.platform.mapping import PlatformChallenge

DEFAULT_BASE_URL = "https://ctf.xidian.edu.cn"
DEFAULT_GAME_ID = 37

# ret2shell limits each account to 10 flag submissions per 5-minute window
# (HTTP 429).  The client-side limiter guards that shared quota.
SUBMIT_WINDOW_SECONDS = 300.0
SUBMIT_LIMIT = 10
# ret2shell judges submissions asynchronously: the POST returns a Submission
# with ``solved: null`` and the result must be polled.
SUBMIT_POLL_ATTEMPTS = 7
SUBMIT_POLL_INTERVAL = 1.0
# Instance exposed_ports appear asynchronously after a start request.
INSTANCE_WAIT_TIMEOUT = 60.0
INSTANCE_WAIT_INTERVAL = 2.0


class Ret2ShellError(RuntimeError):
    pass


class Ret2ShellAuthError(Ret2ShellError):
    pass


class Ret2ShellPreflightError(Ret2ShellError):
    """A submission was refused before it could consume a platform attempt."""


class Ret2ShellRateLimitError(Ret2ShellError):
    pass


class _SubmitRateLimiter:
    """Guard the shared 10-submissions-per-5-minutes account quota."""

    def __init__(
        self,
        *,
        limit: int = SUBMIT_LIMIT,
        window: float = SUBMIT_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._attempts: list[float] = []

    def _trim(self, now: float) -> None:
        self._attempts = [ts for ts in self._attempts if now - ts < self.window]

    def reserve(self) -> None:
        with self._lock:
            now = self._monotonic()
            self._trim(now)
            if len(self._attempts) >= self.limit:
                oldest = min(self._attempts)
                retry_in = self.window - (now - oldest)
                raise Ret2ShellRateLimitError(
                    f"local submit quota exhausted ({self.limit} per "
                    f"{int(self.window)}s); retry in {retry_in:.0f}s"
                )
            self._attempts.append(now)

    def used(self) -> int:
        with self._lock:
            self._trim(self._monotonic())
            return len(self._attempts)


def _error_detail(response: requests.Response) -> str:
    # ret2shell errors are plain-text bodies, not JSON.
    detail = response.text.strip().replace("\r", " ").replace("\n", " ")
    if len(detail) > 500:
        detail = detail[:500] + "..."
    return f"{response.status_code}: {detail}" if detail else str(response.status_code)


class Ret2ShellClient:
    """ret2shell (Rust/axum) participant API client.

    Auth is a JWT Bearer token issued by ``POST /api/account/login`` and
    returned in the ``Set-Token`` response header (never in the body).  Any
    later response may carry a refreshed ``Set-Token``, so every response is
    checked and the token rolled forward.  All timestamps are unix seconds
    and list endpoints return ``[items, total]`` tuples.
    """

    def __init__(
        self,
        base_url: str = "",
        game_id: int | None = None,
        username: str = "",
        password: str = "",
        token: str = "",
        *,
        timeout: float = 30,
        session: requests.Session | None = None,
        poll_attempts: int = SUBMIT_POLL_ATTEMPTS,
        poll_interval: float = SUBMIT_POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = (base_url or os.getenv("IPC_R2S_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
        raw_game_id = game_id if game_id is not None else os.getenv("IPC_R2S_GAME_ID", "")
        try:
            self.game_id = int(raw_game_id) if str(raw_game_id).strip() else DEFAULT_GAME_ID
        except (TypeError, ValueError) as exc:
            raise Ret2ShellError(f"invalid IPC_R2S_GAME_ID: {raw_game_id!r}") from exc
        self.username = username or os.getenv("IPC_R2S_USERNAME", "")
        self.password = password or os.getenv("IPC_R2S_PASSWORD", "")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.poll_attempts = max(1, poll_attempts)
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._token = token or os.getenv("IPC_R2S_TOKEN", "")
        self._limiter = _SubmitRateLimiter(monotonic=monotonic)

    # ---- low-level request handling ----

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api{path}"

    def _absorb_token(self, response: requests.Response) -> None:
        token = response.headers.get("Set-Token")
        if token:
            self._token = token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        stream: bool = False,
        _relogin: bool = True,
    ) -> requests.Response:
        if not self._token:
            self.login()
        response = self.session.request(
            method,
            self._url(path),
            json=json,
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
            stream=stream,
        )
        self._absorb_token(response)
        if response.status_code == 401 and _relogin:
            self._token = ""
            self.login()
            return self._request(
                method, path, json=json, params=params, stream=stream, _relogin=False
            )
        return response

    def _request_ok(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self._request(method, path, json=json, params=params)
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise Ret2ShellError(
                f"unexpected non-JSON response from {path}: {response.text[:200]!r}"
            ) from exc

    # ---- auth ----

    def login(self) -> None:
        if not self.username or not self.password:
            raise Ret2ShellAuthError(
                "ret2shell username/password are not configured; "
                "set IPC_R2S_USERNAME/IPC_R2S_PASSWORD or IPC_R2S_TOKEN"
            )
        response = self.session.post(
            self._url("/account/login"),
            json={
                "account": self.username,
                "password": self.password,
                "captcha_id": "",
                "captcha_answer": "",
            },
            timeout=self.timeout,
        )
        self._absorb_token(response)
        if response.status_code != 200 or not self._token:
            raise Ret2ShellAuthError(f"ret2shell login failed: {_error_detail(response)}")

    # ---- games / challenges ----

    def ping(self) -> str:
        response = self.session.get(self._url("/ping"), timeout=self.timeout)
        if response.status_code != 200:
            raise Ret2ShellError(_error_detail(response))
        return response.text

    def get_profile(self) -> dict[str, Any]:
        return self._request_ok("GET", "/account/profile")

    def get_game(self, game_id: int | None = None) -> dict[str, Any]:
        return self._request_ok("GET", f"/game/{game_id or self.game_id}")

    def list_games(self) -> list[dict[str, Any]]:
        payload = self._request_ok("GET", "/game")
        items = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], list) else payload
        return items if isinstance(items, list) else []

    def list_challenges(self, game_id: int | None = None) -> list[dict[str, Any]]:
        payload = self._request_ok("GET", f"/game/{game_id or self.game_id}/challenge")
        if not (isinstance(payload, list) and payload and isinstance(payload[0], list)):
            raise Ret2ShellError("challenge list response is not an [items, total] tuple")
        return payload[0]

    def get_challenge(self, challenge_id: int, game_id: int | None = None) -> dict[str, Any]:
        return self._request_ok(
            "GET", f"/game/{game_id or self.game_id}/challenge/{challenge_id}"
        )

    def challenge_status(self, challenge_id: int, game_id: int | None = None) -> dict[str, Any]:
        """Own solve state plus the platform-wide solve count."""

        return self._request_ok(
            "GET", f"/game/{game_id or self.game_id}/challenge/{challenge_id}/submit"
        )

    # ---- attachments ----

    def list_files(self, challenge_id: int, game_id: int | None = None) -> list[dict[str, Any]]:
        payload = self._request_ok(
            "GET", f"/game/{game_id or self.game_id}/challenge/{challenge_id}/file"
        )
        if not isinstance(payload, list):
            raise Ret2ShellError("challenge file response is not a list")
        return [item for item in payload if isinstance(item, dict)]

    def download_file(
        self,
        challenge_id: int,
        folder: str,
        file: str,
        dest_dir: str | Path,
        game_id: int | None = None,
        *,
        max_bytes: int | None = None,
    ) -> Path:
        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)
        existing = [path.name for path in destination.iterdir()]
        source = Path(file)
        stem = safe_stem(source.stem, fallback="attachment")
        suffix = source.suffix[:20]
        target = destination / numbered_filename(
            stem, suffix or ".bin", existing, fallback="attachment"
        )
        response = self._request(
            "GET",
            f"/game/{game_id or self.game_id}/challenge/{challenge_id}/file",
            params={"folder": folder, "file": file},
            stream=True,
        )
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))
        written = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise Ret2ShellError(
                            f"attachment exceeds {max_bytes} byte limit: {file}"
                        )
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    # ---- dynamic instances ----

    def list_instances(self, game_id: int | None = None) -> list[dict[str, Any]]:
        payload = self._request_ok("GET", f"/game/{game_id or self.game_id}/instance")
        return payload if isinstance(payload, list) else []

    def find_instance(
        self, challenge_id: int, game_id: int | None = None
    ) -> dict[str, Any] | None:
        for instance in self.list_instances(game_id):
            if instance.get("challenge_id") == challenge_id:
                return instance
        return None

    def start_instance(self, challenge_id: int, game_id: int | None = None) -> None:
        response = self._request(
            "POST", f"/game/{game_id or self.game_id}/challenge/{challenge_id}/instance"
        )
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))

    def renew_instance(self, challenge_id: int, game_id: int | None = None) -> None:
        response = self._request(
            "PATCH", f"/game/{game_id or self.game_id}/challenge/{challenge_id}/instance"
        )
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))

    def destroy_instance(self, challenge_id: int, game_id: int | None = None) -> None:
        response = self._request(
            "DELETE", f"/game/{game_id or self.game_id}/challenge/{challenge_id}/instance"
        )
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))

    def wait_for_instance(
        self,
        challenge_id: int,
        game_id: int | None = None,
        *,
        timeout: float = INSTANCE_WAIT_TIMEOUT,
        interval: float = INSTANCE_WAIT_INTERVAL,
    ) -> dict[str, Any]:
        """Poll until the challenge instance reports reachable exposed_ports."""

        deadline = self._monotonic() + timeout
        last: dict[str, Any] | None = None
        while True:
            instance = self.find_instance(challenge_id, game_id)
            if instance is not None:
                last = instance
                if instance.get("exposed_ports"):
                    return instance
            if self._monotonic() >= deadline:
                state = (last or {}).get("state", "unknown")
                raise Ret2ShellError(
                    f"instance for challenge {challenge_id} did not become reachable "
                    f"within {timeout:.0f}s (last state: {state})"
                )
            self._sleep(interval)

    # ---- flag submission (async judged) ----

    def get_submission(
        self,
        challenge_id: int,
        submission_id: int,
        game_id: int | None = None,
    ) -> dict[str, Any]:
        return self._request_ok(
            "GET",
            f"/game/{game_id or self.game_id}/challenge/{challenge_id}/submit",
            params={"id": submission_id},
        )

    def submit_flag(
        self,
        challenge_id: int,
        flag: str,
        game_id: int | None = None,
        *,
        check_solved: bool = True,
    ) -> dict[str, Any]:
        """Submit one flag and poll the async judge for its verdict.

        Preflight refuses to POST when the challenge is already solved (no
        platform attempt should be spent) and the local limiter protects the
        shared 10-per-5-minutes quota.  Returns the final Submission dict
        with ``solved`` and ``result`` filled in when the judge answered in
        time; ``solved`` stays ``None`` on a poll timeout.
        """

        flag = str(flag).strip()
        if not flag:
            raise Ret2ShellPreflightError("flag must not be empty")
        if check_solved:
            status = self.challenge_status(challenge_id, game_id)
            if isinstance(status, dict) and status.get("solved"):
                raise Ret2ShellPreflightError(
                    f"challenge {challenge_id} is already solved; no submission was sent"
                )
        self._limiter.reserve()
        response = self._request(
            "POST",
            f"/game/{game_id or self.game_id}/challenge/{challenge_id}/submit",
            json={"content": flag},
        )
        if response.status_code == 429:
            raise Ret2ShellRateLimitError(_error_detail(response))
        if response.status_code >= 400:
            raise Ret2ShellError(_error_detail(response))
        submission = response.json() if response.content else {}
        submission_id = submission.get("id")
        if submission_id is None:
            raise Ret2ShellError(f"submit response has no submission id: {submission}")
        for _ in range(self.poll_attempts):
            if submission.get("solved") is not None:
                return submission
            self._sleep(self.poll_interval)
            submission = self.get_submission(challenge_id, submission_id, game_id)
        return submission

    # ---- lifecycle ----

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Ret2ShellClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def primary_tag(challenge: dict[str, Any]) -> str:
    tags = challenge.get("tag")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name"):
                return str(tag["name"])
        if tags and isinstance(tags[0], str):
            return str(tags[0])
    return ""


def normalize_category(raw: str) -> str:
    lowered = raw.strip().lower()
    return lowered if lowered in CATEGORIES else "misc"


class Ret2ShellAdapter(PlatformAdapter):
    """Import ret2shell challenges through the generic platform mechanism."""

    def __init__(
        self,
        client: Ret2ShellClient,
        *,
        game_id: int | None = None,
        category_map: dict[str, str] | None = None,
        max_attachment_bytes: int | None = None,
    ) -> None:
        self.client = client
        self.game_id = game_id or client.game_id
        self.category_map = category_map or {}
        self.max_attachment_bytes = max_attachment_bytes

    def fetch_challenges(self) -> list[PlatformChallenge]:
        challenges: list[PlatformChallenge] = []
        for raw in self.client.list_challenges(self.game_id):
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            raw_category = primary_tag(raw)
            mapped = self.category_map.get(raw_category, raw_category)
            challenges.append(
                PlatformChallenge(
                    external_id=str(raw["id"]),
                    title=str(raw.get("name", "")),
                    category=normalize_category(mapped),
                    description=str(raw.get("content", "") or ""),
                    attachment_urls=[],
                )
            )
        return challenges

    def download_attachments(
        self,
        challenge: PlatformChallenge,
        dest_dir: str | Path,
    ) -> list[Path]:
        # ret2shell attachments are listed per challenge via the file API, so
        # there are no attachment URLs at fetch time.
        downloaded: list[Path] = []
        for item in self.client.list_files(int(challenge.external_id), self.game_id):
            folder = str(item.get("folder", "static"))
            file = item.get("file")
            if not file:
                continue
            downloaded.append(
                self.client.download_file(
                    int(challenge.external_id),
                    folder,
                    str(file),
                    dest_dir,
                    self.game_id,
                    max_bytes=self.max_attachment_bytes,
                )
            )
        return downloaded
