from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import requests

from backend.platform._common import PLATFORM_CATEGORIES
from backend.platform.adapter import PlatformAdapter
from backend.platform.mapping import FieldMapping, PlatformChallenge

_DEFAULT_TIMEOUT = 30

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pwn", (
        "pwn", "pickle", "反序列化", "沙箱", "sandbox", "逃逸", "shellcode",
        "内存破坏", "溢出", "overflow", "eval", "uaf", "heap", "rop",
        "命令执行", "拿到服务器 shell",
    )),
    ("crypto", (
        "crypto", "rsa", "aes", "crc", "lcg", "csidh", "cipher", "mac",
        "spn", "dsa", "otp", "口令", "密钥", "加密", "解密", "密码", "素数",
        "背包", "置换群", "预言机", "bloom", "布隆",
    )),
    ("web", (
        "web", "http", "sql", "waf", "jwt", "spring", "flask", "博客",
        "登录", "注册", "网站", "注入", "表单", "渲染", "ssti", "网关",
        "cookie", "反向代理", "版本库", "git",
    )),
    ("reverse", (
        "逆向", "reverse", "加壳", "packed", "反调试", "二进制", "binary",
        "可执行文件", "verilog", "固件", "文字冒险", "游戏", "crush",
    )),
    ("misc", (
        "取证", "应急响应", "forensics", "流量", "抓包", "邮件", "eml",
        "日志", "隐写", "pcap",
    )),
)


class TSECBenchError(RuntimeError):
    """Classified platform failure; ``reason`` drives scheduler retries."""

    def __init__(
        self, message: str, *, reason: str = "transient", retry_after: int = 60
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = int(retry_after)


class TSECBenchAuthError(TSECBenchError):
    """The benchmark token is missing or was rejected by the task."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="auth")


class TSECBenchPreflightError(TSECBenchError):
    """The task cannot be started as configured (no challenges, no base URL)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="preflight")


class TSECBenchInstanceError(TSECBenchError):
    """Container lifecycle failure with a scheduler-actionable reason."""


def _classify_error(response: Any) -> TSECBenchError:
    """Translate one HTTP failure into a classified error.

    The scheduler reacts differently per reason: a quota or rate limit is
    retried later, an invalid token needs a new credential, a missing
    challenge is permanent, and an ended task rejects the operation outright.
    """

    status = int(getattr(response, "status_code", 0) or 0)
    code = ""
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            message = str(payload.get("message") or "")
    except (ValueError, TypeError):
        pass
    detail = f": {message}" if message else ""
    if status in (401, 403) or code == "task_not_found":
        return TSECBenchAuthError(
            f"the benchmark token was rejected (HTTP {status or 'unknown'}{detail})"
        )
    headers = getattr(response, "headers", {}) or {}
    try:
        retry_after = max(1, int(headers.get("Retry-After", "60")))
    except (TypeError, ValueError):
        retry_after = 60
    if status == 429:
        return TSECBenchError(
            f"the platform rate limited the request{detail}",
            reason="rate_limited", retry_after=retry_after,
        )
    if code == "challenge_not_found":
        reason = "missing"
    elif code == "duplicate":
        reason = "duplicate"
    elif code in ("resource_unavailable", "internal_error"):
        reason = "transient"
    elif code == "invalid_state":
        if any(marker in message for marker in ("上限", "limit", "quota", "活跃")):
            reason = "quota"
        else:
            reason = "ended"
    elif status >= 500:
        reason = "transient"
    elif status == 404:
        reason = "missing"
    elif status == 409:
        reason = "ended"
    else:
        reason = "rejected"
    description = f"TSecBench request failed (HTTP {status or 'unknown'}"
    description += f" {code})" if code else ")"
    return TSECBenchError(
        f"{description}{detail}", reason=reason, retry_after=retry_after
    )


class TSECBenchClient:
    """Minimal OpenAPI client for one benchmark task.

    Authentication is a single ``BENCHMARK_TOKEN`` header; the token scopes
    every request to exactly one task, so there is no login flow, game id or
    session state to maintain.
    """

    PATH_CHALLENGES = "/openapi/v1/challenges"
    PATH_START = "/openapi/v1/challenges/start"
    PATH_HINT = "/openapi/v1/challenges/hint"
    PATH_SUBMIT = "/openapi/v1/challenges/submit"
    PATH_CLOSE = "/openapi/v1/challenges/close"

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()

    def _require_config(self) -> None:
        if not self.base_url:
            raise TSECBenchPreflightError("TSecBench base_url is not configured")
        if not self.token:
            raise TSECBenchAuthError("the benchmark token is not configured")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        self._require_config()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
                headers={
                    "BENCHMARK_TOKEN": self.token,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TSECBenchError(
                f"TSecBench request failed: {exc}", reason="transient"
            ) from exc
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise _classify_error(response)
        if not getattr(response, "content", b""):
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise TSECBenchError(
                "TSecBench returned a non-JSON response", reason="transient"
            ) from exc

    def list_challenges(self) -> list[dict[str, Any]]:
        payload = self._request("GET", self.PATH_CHALLENGES)
        if not isinstance(payload, list):
            raise TSECBenchError(
                "TSecBench challenge list was not a JSON array", reason="transient"
            )
        return [item for item in payload if isinstance(item, dict)]

    def start_container(self, unique_code: str) -> dict[str, Any]:
        payload = self._request(
            "POST", self.PATH_START, params={"unique_code": unique_code}
        )
        return payload if isinstance(payload, dict) else {}

    def close_container(self, unique_code: str) -> dict[str, Any]:
        payload = self._request(
            "POST", self.PATH_CLOSE, params={"unique_code": unique_code}
        )
        return payload if isinstance(payload, dict) else {}

    def hint(self, unique_code: str) -> dict[str, Any]:
        payload = self._request(
            "GET", self.PATH_HINT, params={"unique_code": unique_code}
        )
        return payload if isinstance(payload, dict) else {}

    def submit_flag(self, unique_code: str, flag: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            self.PATH_SUBMIT,
            json_body={"unique_code": unique_code, "flag": flag},
        )
        return payload if isinstance(payload, dict) else {}

    def close(self) -> None:
        closer = getattr(self.session, "close", None)
        if callable(closer):
            closer()


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


class TSECBenchAdapter(PlatformAdapter):
    """Challenge/instance/submit adapter for a TSecBench benchmark task."""

    DEFAULT_INSTANCE_LIMIT = 3

    def __init__(
        self,
        client: TSECBenchClient,
        mapping: FieldMapping,
        *,
        max_attachment_bytes: int | None = None,
        competition_id: str = "",
        team_id: str = "",
    ) -> None:
        if mapping.platform != "tsecbench":
            raise ValueError("TSECBenchAdapter requires a tsecbench FieldMapping")
        self.client = client
        self.mapping = mapping
        self.max_attachment_bytes = max_attachment_bytes
        self.competition_id = str(competition_id or "")
        self.team_id = str(team_id or "")
        self._by_id: dict[str, PlatformChallenge] = {}
        self._instance_views: dict[str, dict[str, Any]] = {}
        self._task_identity = ""

    @property
    def identity(self) -> str:
        scope = f"{self.client.base_url}/benchmark"
        if self.competition_id:
            scope += f"/task/{self.competition_id}"
        if self.team_id:
            scope += f"/team/{self.team_id}"
        if self._task_identity:
            scope += f"/set/{self._task_identity}"
        return scope

    @property
    def supports_submit(self) -> bool:
        return True

    @property
    def supports_instances(self) -> bool:
        return all(
            callable(getattr(self.client, name, None))
            for name in ("start_container", "close_container")
        )

    def instance_limit(self) -> int | None:
        """The platform's documented concurrent-container cap.

        The OpenAPI exposes no limit field, so the published cap (three
        active containers per task) is the only truthful value.
        """
        return self.DEFAULT_INSTANCE_LIMIT

    def _category_override(self, text: str) -> str:
        for keyword, category in self.mapping.category_map.items():
            if str(keyword).casefold() in text:
                value = str(category).strip().lower()
                return value if value in PLATFORM_CATEGORIES else "misc"
        return ""

    def _category(self, raw: dict[str, Any]) -> str:
        field = str(self.mapping.category_field or "").strip()
        if field and raw.get(field) not in (None, ""):
            value = str(raw.get(field)).strip().lower()
            mapped = self._category_override(value) or value
            if mapped in PLATFORM_CATEGORIES:
                return mapped
        haystack = f"{raw.get('unique_code', '')} {raw.get('description', '')}".casefold()
        override = self._category_override(haystack)
        if override:
            return override
        for category, keywords in _CATEGORY_KEYWORDS:
            if any(keyword in haystack for keyword in keywords):
                return category
        return "misc"

    def _normalise(self, raw: dict[str, Any]) -> PlatformChallenge | None:
        external = raw.get("unique_code") or raw.get(self.mapping.id_field)
        if external in (None, ""):
            return None
        container_status = str(raw.get("container_status") or "").strip().lower()
        addresses = [str(item) for item in raw.get("container_addr") or [] if item]
        platform_data: dict[str, Any] = {
            "difficulty": str(raw.get("difficulty") or ""),
            "level": _positive_int(raw.get("level"), 1),
            "score": raw.get("total_score"),
            "total_score": raw.get("total_score"),
            "flag_count": _positive_int(raw.get("flag_count"), 0),
            "correct_flag_count": _positive_int(raw.get("correct_flag_count"), 0),
            "container_status": container_status,
        }
        if addresses:
            platform_data["container_addr"] = addresses
        return PlatformChallenge(
            external_id=str(external),
            title=str(external),
            category=self._category(raw),
            description=str(raw.get("description") or ""),
            attachment_urls=[],
            remote=bool(container_status) or bool(addresses),
            solved=bool(raw.get("is_completed")),
            hints=[],
            platform_data=platform_data,
        )

    def fetch_challenges(self) -> list[PlatformChallenge]:
        raw_challenges = self.client.list_challenges()
        if self.mapping.max_challenges:
            raw_challenges = raw_challenges[: self.mapping.max_challenges]
        result: list[PlatformChallenge] = []
        seen: set[str] = set()
        refreshed: dict[str, PlatformChallenge] = {}
        for raw in raw_challenges:
            challenge = self._normalise(raw)
            if challenge is None:
                continue
            if challenge.external_id in seen:
                raise TSECBenchError(
                    f"duplicate TSecBench challenge id: {challenge.external_id}"
                )
            seen.add(challenge.external_id)
            refreshed[challenge.external_id] = challenge
            result.append(challenge)
        self._by_id = refreshed
        if seen:
            digest = hashlib.sha256(",".join(sorted(seen)).encode()).hexdigest()
            self._task_identity = digest[:12]
        return result

    def challenges(self) -> list[PlatformChallenge]:
        self.fetch_challenges()
        return [item.model_copy(deep=True) for item in self._by_id.values()]

    def preflight(self) -> list[PlatformChallenge]:
        raw = self.client.list_challenges()
        if not raw:
            raise TSECBenchPreflightError(
                "the benchmark task exposes no challenges; verify the benchmark token"
            )
        return self.fetch_challenges()

    def _challenge(self, external_id: str) -> PlatformChallenge:
        if external_id not in self._by_id:
            self.fetch_challenges()
        challenge = self._by_id.get(str(external_id))
        if challenge is None:
            raise TSECBenchError(
                f"unknown TSecBench challenge: {external_id}", reason="missing"
            )
        return challenge

    @staticmethod
    def _instance_view(payload: Any) -> dict[str, Any]:
        addresses = [
            str(item) for item in (payload or {}).get("container_addr") or [] if item
        ]
        view: dict[str, Any] = {"state": "ready" if addresses else "pending"}
        if addresses:
            view["entry"] = addresses[0]
            view["url"] = addresses[0]
            view["addresses"] = addresses
        return view

    def instances(self) -> list[dict[str, Any]]:
        """Report the containers this task currently holds.

        The list endpoint carries the live ``container_status`` for every
        challenge, which is the only place TSecBench exposes an existing
        instance; the reconciliation reader uses it to retire orphans.
        """
        if not self.supports_instances:
            return []
        found: list[dict[str, Any]] = []
        for challenge in self.challenges():
            data = challenge.platform_data or {}
            addresses = [str(item) for item in data.get("container_addr") or [] if item]
            if str(data.get("container_status") or "") == "available" and addresses:
                found.append(
                    {
                        "external_id": challenge.external_id,
                        **self._instance_view({"container_addr": addresses}),
                    }
                )
        return found

    def start_instance(self, external_id: str) -> dict[str, Any]:
        self._challenge(external_id)
        if not self.supports_instances:
            raise ValueError("TSecBench client does not expose the container lifecycle")
        try:
            payload = self.client.start_container(external_id)
        except TSECBenchError as exc:
            raise TSECBenchInstanceError(
                str(exc), reason=exc.reason, retry_after=exc.retry_after
            ) from exc
        view = self._instance_view(payload)
        if view["state"] != "ready":
            raise TSECBenchInstanceError(
                f"TSecBench returned no container address for challenge {external_id}",
                reason="transient", retry_after=30,
            )
        self._instance_views[str(external_id)] = view
        return dict(view)

    def stop_instance(self, external_id: str) -> None:
        if not self.supports_instances:
            return
        try:
            self.client.close_container(external_id)
        except TSECBenchError as exc:
            if exc.reason == "missing":
                return
            raise TSECBenchInstanceError(
                str(exc), reason=exc.reason, retry_after=exc.retry_after
            ) from exc
        self._instance_views.pop(str(external_id), None)

    def renew_instance(self, external_id: str) -> dict[str, Any]:
        """Confirm a running container; TSecBench has no lease to extend.

        Containers live until closed or the task ends, so renewal is a
        truthful re-read of the challenge list instead: a container that
        silently stopped is reported expired so the scheduler repairs it.
        """
        if not self.supports_instances:
            raise ValueError("TSecBench client does not expose the container lifecycle")
        container_status = ""
        addresses: list[str] = []
        for raw in self.client.list_challenges():
            if str(raw.get("unique_code") or "") == str(external_id):
                container_status = str(raw.get("container_status") or "").strip().lower()
                addresses = [
                    str(item) for item in raw.get("container_addr") or [] if item
                ]
                break
        if container_status == "available" and addresses:
            view = self._instance_view({"container_addr": addresses})
            self._instance_views[str(external_id)] = view
            return dict(view)
        raise TSECBenchInstanceError(
            f"TSecBench container for challenge {external_id} is "
            f"{container_status or 'gone'}",
            reason="expired", retry_after=30,
        )

    def rebuild_instance(self, external_id: str) -> dict[str, Any]:
        """Replace a container that could not be confirmed as running."""
        try:
            self.client.close_container(external_id)
        except TSECBenchError:
            pass
        return self.start_instance(external_id)

    def submit(self, external_id: str, flag: str) -> dict[str, Any]:
        try:
            payload = self.client.submit_flag(external_id, flag)
        except TSECBenchError as exc:
            if exc.reason == "auth":
                return {"verdict": "auth_required"}
            if exc.reason == "duplicate":
                return {"verdict": "correct", "reason": str(exc)}
            if exc.reason == "rate_limited":
                return {
                    "verdict": "rate_limited",
                    "retry_after": getattr(exc, "retry_after", 60),
                }
            if exc.reason == "transient":
                return {"verdict": "unknown", "reason": str(exc)}
            return {"verdict": "rejected", "reason": str(exc)}
        return self._normalise_submission(payload)

    @staticmethod
    def _normalise_submission(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"verdict": "unknown"}
        correct = payload.get("correct")
        if correct is True:
            return {
                "verdict": "correct",
                "awarded": payload.get("awarded"),
                "cumulative_score": payload.get("cumulative_score"),
                "correct_flag_count": payload.get("correct_flag_count"),
                "total_flag_count": payload.get("total_flag_count"),
                "matched_flag_index": payload.get("matched_flag_index"),
            }
        if correct is False:
            return {"verdict": "wrong"}
        return {"verdict": "unknown"}

    def hint(self, external_id: str) -> dict[str, Any]:
        """Read the optional hint. Reading it discounts later flag scores."""
        try:
            return self.client.hint(external_id)
        except TSECBenchError as exc:
            return {"error": str(exc), "reason": getattr(exc, "reason", "transient")}

    def download_attachments(
        self, challenge: PlatformChallenge, dest_dir: str | Path
    ) -> list[Path]:
        """TSecBench exposes no attachment endpoint.

        Challenge material (scripts, captures, binaries) lives inside each
        challenge container and is served from the live target after the
        container starts, so the import stage has nothing to download.
        """
        return []
