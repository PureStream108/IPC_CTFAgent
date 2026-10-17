from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests
from typing_extensions import Self

from backend.platform._common import PLATFORM_CATEGORIES, numbered_filename, safe_stem
from backend.platform.adapter import PlatformAdapter
from backend.platform.mapping import FieldMapping, PlatformChallenge

_recovery_logger = logging.getLogger(__name__)

class GZCTFError(RuntimeError):
    pass


class GZCTFLoginError(GZCTFError):
    reason = "auth_required"


class GZCTFInstanceError(GZCTFError):
    """A classified lifecycle failure; a failed renewal never implies destruction."""

    def __init__(self, message: str, *, reason: str, retry_after: int = 60):
        super().__init__(message)
        self.reason = reason
        self.retry_after = max(5, min(3600, retry_after))


class GZCTFPreflightError(GZCTFError):
    """A submission was refused before it could consume a platform attempt."""


def _track_id(item: dict[str, Any]) -> str:
    for key in ("trackId", "track_id", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _track_entries(challenge: dict[str, Any]) -> list[dict[str, Any]]:
    entries = challenge.get("tracks")
    if isinstance(entries, list):
        flags = challenge.get("flags") if isinstance(challenge.get("flags"), list) else []
        result: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict) or not _track_id(item):
                continue
            joined = dict(item)
            if not isinstance(joined.get("levels"), list):
                joined["levels"] = [
                    flag
                    for flag in flags
                    if isinstance(flag, dict) and _track_id(flag) == _track_id(item)
                ]
            result.append(joined)
        return result
    entries = challenge.get("flags")
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict) and _track_id(item)]
    return []


def _level_entries(track: dict[str, Any]) -> list[dict[str, Any]]:
    entries = track.get("levels")
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict)]
    entries = track.get("flags")
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict)]
    return []


def _find_level(track: dict[str, Any], level: int) -> dict[str, Any] | None:
    for item in _level_entries(track):
        raw = item.get("level", item.get("levelId"))
        try:
            if int(raw) == level:
                return item
        except (TypeError, ValueError):
            continue
    return None


def _parse_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
            seconds = float(value)
            if seconds > 100_000_000_000:
                seconds /= 1000
            return datetime.fromtimestamp(seconds, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _active_cooldown(*objects: dict[str, Any]) -> str | None:
    now = datetime.now(UTC)
    for obj in objects:
        cooldown = obj.get("cooldown")
        if isinstance(cooldown, dict):
            value = cooldown.get("cooldownUntil")
        else:
            value = obj.get("cooldownUntil")
        until = _parse_time(value)
        if until and until > now:
            return str(value)
    return None


def _normalize_answer(answer: Any) -> str:
    return str(answer).strip()


def validate_submission(
    challenge: dict[str, Any],
    *,
    level: int,
    answer: Any,
    track_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one submission against live challenge state.

    This is intentionally conservative.  It refuses ambiguous tracks,
    locked/solved levels, active cooldowns, unverified evidence and
    malformed answers before the POST endpoint is called.
    """

    try:
        level = int(level)
    except (TypeError, ValueError) as exc:
        raise GZCTFPreflightError("level must be a positive integer") from exc
    normalized_answer = _normalize_answer(answer)
    if not normalized_answer:
        raise GZCTFPreflightError("answer must not be empty")

    challenge_lock = challenge.get("challengeLock", challenge.get("challengeLocked", False))
    if isinstance(challenge_lock, dict):
        challenge_lock = challenge_lock.get(
            "isLocked", challenge_lock.get("locked", challenge_lock.get("is_locked", False))
        )
    if bool(challenge_lock):
        raise GZCTFPreflightError("challenge is currently locked")

    tracks = _track_entries(challenge)
    enabled = [item for item in tracks if item.get("isEnabled", item.get("is_enabled", True))]
    if not enabled:
        if not isinstance(challenge.get("context"), dict):
            raise GZCTFPreflightError("live challenge has no enabled track state")
        if bool(challenge.get("isSolved", challenge.get("solved", False))):
            raise GZCTFPreflightError("challenge is already solved")
        cooldown = _active_cooldown(challenge)
        if cooldown:
            raise GZCTFPreflightError(f"submission cooldown active until {cooldown}")
        return {
            "ok": True,
            "challenge_id": challenge.get("id"),
            "track_id": str(track_id or "") or None,
            "level": level,
            "answer": normalized_answer,
            "status": "unsolved",
            "current_level": None,
        }
    if track_id:
        track = next((item for item in enabled if _track_id(item) == str(track_id)), None)
        if track is None:
            raise GZCTFPreflightError(f"trackId is not an enabled track: {track_id}")
    elif len(enabled) == 1:
        track = enabled[0]
        track_id = _track_id(track)
    else:
        raise GZCTFPreflightError(
            "challenge exposes multiple enabled tracks; an explicit trackId is required"
        )

    level_state = _find_level(track, level)
    if level_state is None:
        raise GZCTFPreflightError(f"track {track_id} has no level {level} state")
    status = str(level_state.get("status", level_state.get("state", ""))).strip().lower()
    if status in {"solved", "complete", "completed"} or bool(level_state.get("solved")):
        raise GZCTFPreflightError(f"level {level} is already solved")
    if status in {"locked", "unavailable", "banned"}:
        raise GZCTFPreflightError(f"level {level} is locked")
    current_level = track.get("currentLevel", track.get("current_level"))
    try:
        if current_level is not None and level > int(current_level):
            raise GZCTFPreflightError(
                f"level {level} is locked until level {int(current_level)} is solved"
            )
    except (TypeError, ValueError):
        pass

    cooldown = _active_cooldown(challenge, track, level_state)
    if cooldown:
        raise GZCTFPreflightError(f"submission cooldown active until {cooldown}")
    cooldown_obj = level_state.get("cooldown")
    if not isinstance(cooldown_obj, dict):
        cooldown_obj = track.get("cooldown") if isinstance(track.get("cooldown"), dict) else challenge.get("cooldown", {})
    if isinstance(cooldown_obj, dict):
        wrong_count = cooldown_obj.get("wrongCount", cooldown_obj.get("wrong_count"))
        wrong_limit = cooldown_obj.get("wrongAnswerLimit", cooldown_obj.get("wrong_answer_limit"))
        try:
            if wrong_count is not None and wrong_limit is not None and int(wrong_count) >= int(wrong_limit):
                raise GZCTFPreflightError("wrong-answer limit reached; wait for the platform cooldown")
        except (TypeError, ValueError):
            pass

    if evidence is not None:
        status_value = str(evidence.get("status", "candidate")).strip().lower()
        answers = evidence.get("answers") if isinstance(evidence.get("answers"), dict) else {}
        evidence_answer = answers.get(str(level), answers.get(level))
        if evidence_answer is None or _normalize_answer(evidence_answer) != normalized_answer:
            raise GZCTFPreflightError("submitted answer does not match the evidence report")
        if status_value not in {"verified", "confirmed"}:
            raise GZCTFPreflightError("candidate evidence has not been verified")

    return {
        "ok": True,
        "challenge_id": challenge.get("id"),
        "track_id": track_id,
        "level": level,
        "answer": normalized_answer,
        "status": status or "unsolved",
        "current_level": current_level,
    }


class GZCTFClient:
    """Cookie-authenticated GZCTF participant API client.

    GZCTF flag submission requires the normal user cookie session, not the
    admin-only API token. This client keeps that distinction explicit.
    """

    def __init__(
        self,
        base_url: str = "",
        username: str = "",
        password: str = "",
        token: str = "",
        *,
        timeout: float = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("IPC_GZ_BASE_URL", "")).rstrip("/")
        self.username = username or os.getenv("IPC_GZ_USERNAME", "")
        self.password = password or os.getenv("IPC_GZ_PASSWORD", "")
        self.token = token or os.getenv("IPC_GZ_TOKEN", "")
        self.timeout = timeout
        self.session = requests.Session()
        self.logged_in = False
        self._api_public_key: str | None = None
        if self.token:
            self._install_token(self.token)
            self.logged_in = True

    def _install_token(self, token: str) -> None:
        value = str(token).strip()
        for prefix in ("GZCTF_Token:", "GZCTF_Token="):
            if value.lower().startswith(prefix.lower()):
                value = value[len(prefix):].strip()
                break
        if ";" in value:
            value = value.split(";", 1)[0].strip()
        if not value:
            return
        hostname = urlsplit(self.base_url).hostname
        if hostname:
            self.session.cookies.set("GZCTF_Token", value, domain=hostname, path="/")
        else:
            self.session.cookies.set("GZCTF_Token", value, path="/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _require_base_url(self) -> None:
        if not self.base_url:
            raise GZCTFError("GZCTF base_url is not configured; set IPC_GZ_BASE_URL")

    def _encrypt_api_data(self, value: str) -> str:
        """Encrypt password/flag payloads when the deployment enables it.

        GZCTF publishes an X25519 public key through ``/api/config``. The
        browser derives an AES-GCM key from an ephemeral X25519 exchange and
        sends ``ephemeral_public || nonce || ciphertext_and_tag`` as standard
        base64. Keep this wire detail inside the native adapter so credentials
        and flags never enter workflow templates or generic HTTP code.
        """

        if self._api_public_key is None:
            response = self.session.get(self._url("/api/config"), timeout=self.timeout)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise GZCTFError("GZCTF config endpoint did not return JSON") from exc
            key = payload.get("apiPublicKey") if isinstance(payload, dict) else None
            self._api_public_key = str(key or "")
        if not self._api_public_key:
            return value
        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import (
                X25519PrivateKey,
                X25519PublicKey,
            )
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )

            recipient = X25519PublicKey.from_public_bytes(
                base64.b64decode(self._api_public_key)
            )
            ephemeral = X25519PrivateKey.generate()
            shared_secret = ephemeral.exchange(recipient)
            aes_key = hashlib.sha256(shared_secret).digest()
            nonce = secrets.token_bytes(12)
            encrypted = AESGCM(aes_key).encrypt(nonce, value.encode("utf-8"), None)
            public_bytes = ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            return base64.b64encode(public_bytes + nonce + encrypted).decode("ascii")
        except (ValueError, TypeError) as exc:
            raise GZCTFError("GZCTF API encryption key is invalid") from exc
        except ImportError as exc:
            raise GZCTFError(
                "GZCTF API encryption requires the cryptography package"
            ) from exc

    def login(self) -> None:
        self._require_base_url()
        if self.token:
            self._install_token(self.token)
            self.logged_in = True
            return
        if not self.username or not self.password:
            raise GZCTFLoginError("GZCTF username/password are not configured")
        response = self.session.post(
            self._url("/api/account/login"),
            json={"userName": self.username, "password": self._encrypt_api_data(self.password)},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            try:
                message = response.json().get("title", "login failed")
            except ValueError:
                message = "login failed"
            raise GZCTFLoginError(message)
        self.logged_in = True

    def _authorized_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self._require_base_url()
        if not self.logged_in:
            self.login()
        response = self.session.get(self._url(path), params=params, timeout=self.timeout)
        if response.status_code == 401:
            self.logged_in = False
            self.token = ""
            if self.username and self.password:
                self.login()
                response = self.session.get(self._url(path), params=params, timeout=self.timeout)
            if response.status_code == 401:
                raise GZCTFLoginError("GZCTF_Token is invalid or expired; provide a new token")
        if response.status_code == 403:
            self.logged_in = False
            raise GZCTFLoginError("GZCTF account is not authorized for this GZCTF game")
        response.raise_for_status()
        return response.json()

    def _authorized_post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self._require_base_url()
        if not self.logged_in:
            self.login()
        response = self.session.post(
            self._url(path),
            json=json or {},
            params=params,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            self.logged_in = False
            self.token = ""
            if self.username and self.password:
                self.login()
                response = self.session.post(
                    self._url(path), json=json or {}, params=params, timeout=self.timeout
                )
            if response.status_code == 401:
                raise GZCTFLoginError("GZCTF_Token is invalid or expired; provide a new token")
        if response.status_code == 403:
            self.logged_in = False
            raise GZCTFLoginError("GZCTF account is not authorized for this GZCTF game")
        if response.status_code >= 400:
            detail = response.text.strip().replace("\r", " ").replace("\n", " ")
            if len(detail) > 1000:
                detail = detail[:1000] + "..."
            suffix = f": {detail}" if detail else ""
            raise requests.HTTPError(
                f"{response.status_code} Client Error for url: {response.url}{suffix}",
                response=response,
            )
        return response.json()

    def list_games(self, *, query: dict[str, Any] | None = None) -> Any:
        """List visible competitions without selecting one implicitly."""

        return self._authorized_get("/api/game", params=query)

    def list_teams(self) -> Any:
        return self._authorized_get("/api/team")

    def create_team(self, payload: dict[str, Any]) -> Any:
        return self._authorized_post("/api/team", payload)

    def accept_team_invite(self, payload: dict[str, Any]) -> Any:
        return self._authorized_post("/api/team/accept", payload)

    def join_game(self, game_id: int, payload: dict[str, Any] | None = None) -> Any:
        return self._authorized_post(f"/api/game/{game_id}", payload or {})

    def leave_game(self, game_id: int) -> Any:
        self._require_base_url()
        if not self.logged_in:
            self.login()
        response = self.session.delete(
            self._url(f"/api/game/{game_id}"), timeout=self.timeout
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def get_profile(self) -> dict[str, Any]:
        return self._authorized_get("/api/Account/Profile")


    def create_container(self, game_id: int, challenge_id: int) -> dict[str, Any]:
        result = self._authorized_post(
            f"/api/Game/{int(game_id)}/Container/{int(challenge_id)}"
        )
        return result if isinstance(result, dict) else {}

    def extend_container(self, game_id: int, challenge_id: int) -> dict[str, Any]:
        result = self._authorized_post(
            f"/api/Game/{int(game_id)}/Container/{int(challenge_id)}/Extend"
        )
        return result if isinstance(result, dict) else {}

    def delete_container(self, game_id: int, challenge_id: int) -> None:
        self._require_base_url()
        if not self.logged_in:
            self.login()
        response = self.session.delete(
            self._url(f"/api/Game/{int(game_id)}/Container/{int(challenge_id)}"),
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return
        if response.status_code == 400 and "未创建容器" in response.text:
            return
        response.raise_for_status()
        return

    def get_container(self, game_id: int, challenge_id: int) -> dict[str, Any]:
        """Read the live container state from the challenge detail.

        GZCTF has no dedicated container status endpoint: the instance entry
        and its close time live in the challenge's ``context``.
        """

        detail = self.get_challenge(int(game_id), int(challenge_id))
        context = detail.get("context") if isinstance(detail, dict) else None
        return context if isinstance(context, dict) else {}

    def get_game(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}")

    def get_game_details(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Details")

    def get_game_check(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Check")

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        """Extract challenge objects from the list shapes used by GZCTF.

        Deployments have returned a bare array, an ``items``/``data`` object,
        and a paginated ``{data: {items: [...]}}`` object over time.  Keeping
        the normalization here means the adapter never guesses from HTML or
        treats a successful envelope as a challenge.
        """
        candidates: list[Any] = [payload]
        if isinstance(payload, dict):
            for key in ("challenges", "items", "data", "results"):
                if key in payload:
                    candidates.append(payload[key])
        for candidate in candidates:
            if isinstance(candidate, dict):
                for key in ("items", "challenges", "results"):
                    if isinstance(candidate.get(key), list):
                        candidate = candidate[key]
                        break
                else:
                    for key in ("challenges", "items", "results", "data"):
                        grouped = candidate.get(key)
                        if isinstance(grouped, dict):
                            flattened = [
                                item
                                for values in grouped.values()
                                if isinstance(values, list)
                                for item in values
                                if isinstance(item, dict)
                            ]
                            if flattened:
                                return flattened
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    def list_challenges(self, game_id: int | None = None) -> list[dict[str, Any]]:
        """Return visible challenges without silently selecting a game.

        The public endpoint is preferred.  A few older GZCTF versions only
        expose challenges inside ``Game/Details``; that shape is accepted as a
        compatibility fallback, while an empty/malformed response remains an
        empty list instead of fabricating challenge rows.
        """
        selected = int(game_id or 0)
        if selected <= 0:
            raise GZCTFError("a positive game_id is required to list challenges")
        try:
            payload = self._authorized_get(f"/api/Game/{selected}/Challenges")
            items = self._items(payload)
            if items or isinstance(payload, list) or (
                isinstance(payload, dict)
                and any(key in payload for key in ("challenges", "items", "data", "results"))
            ):
                return items
        except (requests.HTTPError, ValueError):
            pass
        details = self.get_game_details(selected)
        items = self._items(details)
        if items or isinstance(details, list) or (
            isinstance(details, dict)
            and any(key in details for key in ("challenges", "items", "data", "results"))
        ):
            return items
        raise GZCTFError("GZCTF challenge response did not contain a challenge list")

    def get_game_participation(self, game_id: int) -> Any:
        return self._authorized_get(f"/api/game/{game_id}/participations")

    def get_scoreboard(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Scoreboard")

    def get_public_scoreboard(self, game_id: int) -> dict[str, Any]:
        """Read the public scoreboard without creating a participant session.

        GZCTF exposes scoreboard metadata for practice games without login,
        while challenge details and submissions still require the normal
        participant cookie. Keeping this explicitly read-only prevents a
        discovery smoke test from pretending it can submit flags.
        """

        self._require_base_url()
        response = self.session.get(
            self._url(f"/api/Game/{int(game_id)}/Scoreboard"),
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise GZCTFError("public GZCTF scoreboard did not return JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("challenges"), dict):
            raise GZCTFError("public GZCTF scoreboard has no challenge categories")
        return payload

    def get_challenge(
        self,
        game_id: int,
        challenge_id: int,
    ) -> dict[str, Any]:
        return self._authorized_get(
            f"/api/Game/{game_id}/Challenges/{challenge_id}"
        )

    def preflight_submission(
        self,
        game_id: int,
        challenge_id: int,
        *,
        level: int,
        answer: Any,
        track_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch live state and validate without issuing a submission POST."""

        challenge = self.get_challenge(game_id, challenge_id)
        return validate_submission(
            challenge,
            level=level,
            answer=answer,
            track_id=track_id,
            evidence=evidence,
        )

    def submit_level(
        self,
        game_id: int,
        challenge_id: int,
        answer: Any,
        *,
        level: int = 1,
        track_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run preflight immediately before the single allowed POST."""

        decision = self.preflight_submission(
            game_id,
            challenge_id,
            level=level,
            answer=answer,
            track_id=track_id,
            evidence=evidence,
        )
        return self.submit_flag(
            game_id,
            challenge_id,
            decision["answer"],
            level=decision["level"],
            track_id=decision["track_id"],
        )

    def submit_flag(
        self,
        game_id: int,
        challenge_id: int,
        flag: str,
        *,
        level: int = 1,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"flag": self._encrypt_api_data(flag)}
        result = self._authorized_post(
            f"/api/Game/{game_id}/Challenges/{challenge_id}",
            body,
        )
        if isinstance(result, int):
            result = {"id": result, "status": "pending"}
        if not isinstance(result, (dict, str)):
            raise GZCTFError(
                f"GZCTF submit_flag returned unexpected type: {type(result).__name__}"
            )
        return result

    def get_submission_status(
        self,
        game_id: int,
        challenge_id: int,
        submission_id: int,
    ) -> dict[str, Any]:
        return self._authorized_get(
            f"/api/Game/{game_id}/Challenges/{challenge_id}/Status/{submission_id}"
        )

    def get_my_submissions(self, *, query: dict[str, Any] | None = None) -> Any:
        return self._authorized_get("/api/ext/submissions/mine", params=query)

    def get_my_tickets(self, *, query: dict[str, Any] | None = None) -> Any:
        return self._authorized_get("/api/ext/tickets/mine", params=query)

    def get_ticket(self, ticket_id: int | str) -> Any:
        return self._authorized_get(f"/api/ext/tickets/{ticket_id}")

    def create_ticket(self, payload: dict[str, Any]) -> Any:
        return self._authorized_post("/api/ext/tickets", payload)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _mapping_value(item: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = item
    if not path:
        return default
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return default
    return current


def _first_value(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = _mapping_value(item, name, None)
        if value is not None:
            return value
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "solved", "complete", "completed", "accepted", "correct"}
    return False


_GZ_CONTAINER_TYPES = {
    "dynamiccontainer",
    "dynamicattachmentwithcontainer",
    "containerdynamic",
    "containerstatic",
    "dynamicattachment",
}
_GZ_STATIC_TYPES = {"staticattachment", "staticcontainer", "static"}


def _gz_container_type(raw: dict[str, Any]) -> str | None:
    """Identify a per-team container challenge from its detail payload.

    GZCTF spells the challenge type differently across versions, and some
    deployments only reveal the container nature through the exposed port or a
    live instance entry. Any one of those is enough; the name of whichever
    signal fired is returned so the scheduler can record why.
    """

    declared = _first_value(raw, "type", "challengeType", "challenge_type")
    if declared is not None:
        normalised = str(declared).replace("_", "").replace("-", "").strip().lower()
        if normalised in _GZ_CONTAINER_TYPES:
            return str(declared)
        if normalised in _GZ_STATIC_TYPES:
            return None
    for key in ("containerExposePort", "container_expose_port", "exposePort"):
        port = _mapping_value(raw, key, None)
        try:
            if port is not None and int(port) > 0:
                return key
        except (TypeError, ValueError):
            continue
    context = _mapping_value(raw, "context", None)
    if isinstance(context, dict):
        for key in ("instanceEntry", "entry", "closeTime"):
            if context.get(key):
                return f"context.{key}"
    return None


def _gz_solve_count(raw: dict[str, Any]) -> int | None:
    """Read how many teams already solved a challenge, if the API says so."""

    value = _first_value(
        raw, "solved", "solvedCount", "solved_count", "acceptedCount",
        "accepted_count", "solveCount", "solve_count",
    )
    if isinstance(value, bool) or value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _gz_instance_view(payload: Any) -> dict[str, Any]:
    """Normalise a GZCTF container payload into the scheduler's shape.

    The scheduler only understands ``state``/``entry``/``expires_at``; GZCTF
    reports the address as ``instanceEntry`` and the expiry as ``closeTime``,
    nested in ``context`` on some responses and top level on others. Without an
    entry there is nothing an agent could connect to, so that case is reported
    as pending rather than ready.
    """

    if not isinstance(payload, dict):
        return {"state": "pending"}
    context = payload.get("context")
    source = context if isinstance(context, dict) else payload
    entry = _first_value(source, "instanceEntry", "instance_entry", "entry", "url")
    expires = _first_value(source, "closeTime", "close_time", "expiresAt", "expires_at")
    view: dict[str, Any] = {"state": "ready" if entry else "pending"}
    if entry:
        view["entry"] = str(entry)
        view["url"] = str(entry)
    expiry = _parse_time(expires)
    if expiry is not None:
        view["expires_at"] = expiry.isoformat().replace("+00:00", "Z")
        view["renew_before_seconds"] = 300
    return view


def _gz_container_error(exc: requests.HTTPError, external_id: str) -> Exception:
    """Translate a container HTTP failure into a classified error.

    The scheduler reacts differently to each: a quota or rate limit should be
    retried later, an auth failure needs a new token, and a missing challenge
    is permanent.
    """

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", 0)
    if status in (401, 403):
        return GZCTFLoginError(
            "GZCTF container request was not authorized; provide a new token"
        )
    detail = ""
    try:
        detail = (response.text or "").strip()[:300] if response is not None else ""
    except Exception:
        _recovery_logger.debug('Error handled in _gz_container_error', exc_info=True)
        detail = ""
    headers = getattr(response, "headers", {}) or {}
    try:
        retry_after = int(headers.get("Retry-After", "60"))
    except (ValueError, TypeError):
        retry_after = 60
    reason = "transient"
    if status == 429:
        reason = "rate_limited"
    elif status == 404:
        reason = "missing"
    elif status == 400 and any(token in detail.casefold() for token in (
        "尚不可延长", "尚不能延长", "不可续期", "未到续期", "too early", "not yet", "cannot be extended yet",
    )):
        reason = "too_early"
    elif any(token in detail.casefold() for token in ("expired", "not found", "not created", "已过期", "不存在")):
        reason = "expired"
    suffix = f": {detail}" if detail else ""
    if status == 429:
        description = f"GZCTF rate limited the container request for challenge {external_id}"
    elif status == 404:
        description = f"GZCTF container challenge {external_id} does not exist"
    else:
        description = f"GZCTF container request for challenge {external_id} failed with status {status or 'unknown'}"
    return GZCTFInstanceError(
        f"{description}{suffix}",
        reason=reason, retry_after=retry_after,
    )


_ANSWER_RESULT_VERDICTS = {
    "correctanswer": "correct",
    "wronganswer": "wrong",
    "cheatdetected": "rejected",
    "pending": "pending",
    "correct": "correct",
    "accepted": "correct",
    "solved": "correct",
    "success": "correct",
    "complete": "correct",
    "completed": "correct",
    "wrong": "wrong",
    "incorrect": "wrong",
    "failed": "wrong",
    "fail": "wrong",
    "rejected": "rejected",
    "queued": "pending",
    "judging": "pending",
    "processing": "pending",
    "running": "pending",
}


def _normalise_gz_verdict(payload: Any, *, submission_id: str | None = None) -> dict[str, Any]:
    """Convert GZCTF's version-dependent result object to the competition contract."""
    if isinstance(payload, str):
        verdict = _ANSWER_RESULT_VERDICTS.get(payload.strip().lower())
        return {"verdict": verdict or "unknown", "submission_id": submission_id}
    if not isinstance(payload, dict):
        return {"verdict": "unknown", "submission_id": submission_id}
    identifier = submission_id
    for key in ("submissionId", "submission_id", "id", "ticketId"):
        if payload.get(key) is not None:
            identifier = str(payload[key])
            break
    for key in ("solved", "isSolved", "correct", "isCorrect", "accepted", "success"):
        if key in payload and isinstance(payload[key], bool):
            return {"verdict": "correct" if payload[key] else "wrong", "submission_id": identifier}
    raw = _first_value(payload, "verdict", "status", "state", "result", default="")
    status = str(raw).strip().lower()
    if status in {"correct", "accepted", "solved", "success", "complete", "completed", "correctanswer"}:
        verdict = "correct"
    elif status in {"wrong", "incorrect", "failed", "fail", "wronganswer"}:
        verdict = "wrong"
    elif status in {"rejected", "cheatdetected"}:
        verdict = "rejected"
    elif status in {"pending", "queued", "judging", "processing", "running"}:
        verdict = "pending"
    else:
        verdict = "pending" if identifier and any(key in payload for key in ("createdAt", "submittedAt")) else "unknown"
    return {"verdict": verdict, "submission_id": identifier}


class GZCTFAdapter(PlatformAdapter):
    """Unified challenge/submit adapter for a participant GZCTF session."""

    def __init__(
        self,
        client: GZCTFClient,
        mapping: FieldMapping,
        *,
        max_attachment_bytes: int | None = None,
        competition_id: str = "",
        team_id: str = "",
    ) -> None:
        if mapping.platform != "gzctf":
            raise ValueError("GZCTFAdapter requires a gzctf FieldMapping")
        if mapping.game_id is None:
            raise ValueError("gzctf mapping requires game_id")
        self.client = client
        self.mapping = mapping
        self.game_id = int(mapping.game_id)
        self.max_attachment_bytes = max_attachment_bytes
        self.competition_id = str(competition_id or "")
        self.team_id = str(team_id or "")
        self.account_identity = ""
        self.team_identity = ""
        self._by_id: dict[str, PlatformChallenge] = {}

    @property
    def identity(self) -> str:
        scope = f"{self.client.base_url}/game/{self.game_id}"
        if self.competition_id:
            scope += f"/competition/{self.competition_id}"
        if self.team_id:
            scope += f"/team/{self.team_id}"
        if self.account_identity:
            scope += f"/account/{self.account_identity}"
        return scope

    @staticmethod
    def _ids(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, dict):
            for key in (
                "id", "teamId", "team_id", "teamID", "teamName", "team_name",
                "name", "userId", "user_id",
            ):
                item = value.get(key)
                if item is not None:
                    result.add(str(item))
            for key in ("team", "teams", "participation", "participations", "data", "items"):
                if key in value:
                    result.update(GZCTFAdapter._ids(value[key]))
        elif isinstance(value, list):
            for item in value:
                result.update(GZCTFAdapter._ids(item))
        return result

    def _verify_scope(self) -> None:
        profile = self.client.get_profile()
        account = _first_value(profile, "id", "userId", "user_id", "userName", "username", default="")
        if account:
            self.account_identity = str(account)
        if not self.team_id:
            return
        game_getter = getattr(self.client, "get_game", None)
        if callable(game_getter):
            game = game_getter(self.game_id)
            if self.team_id in self._ids(game):
                self.team_identity = self.team_id
                return
        participation_getter = getattr(self.client, "get_game_participation", None)
        if callable(participation_getter):
            try:
                participation = participation_getter(self.game_id)
                team_ids = self._ids(participation)
            except GZCTFLoginError:
                details_getter = getattr(self.client, "get_game_details", None)
                if not callable(details_getter):
                    raise
                details = details_getter(self.game_id)
                rank = details.get("rank") if isinstance(details, dict) else None
                team_ids = self._ids(rank) if isinstance(rank, dict) else set()
            if self.team_id not in team_ids:
                raise GZCTFPreflightError(
                    f"current GZCTF account is not a member of team {self.team_id} for game {self.game_id}"
                )
            self.team_identity = self.team_id

    @property
    def supports_submit(self) -> bool:
        return True

    @property
    def supports_instances(self) -> bool:
        """Whether this client can own per-team containers.

        Detected from the client rather than hardcoded: an older client object
        (and every lightweight test double) has no container methods, and
        claiming the capability there would make the scheduler reserve a seat
        for a side effect that can never succeed.
        """
        return all(
            callable(getattr(self.client, name, None))
            for name in ("create_container", "extend_container", "delete_container")
        )

    def instance_limit(self) -> int | None:
        """How many containers this team may hold at once, per the platform.

        ``None`` means the platform did not say, in which case the caller falls
        back to the workflow's ``remote_instance_limit``.
        """
        getter = getattr(self.client, "get_game", None)
        if not callable(getter):
            return None
        try:
            game = getter(self.game_id)
        except (GZCTFError, requests.RequestException, ValueError, TypeError):
            return None
        if not isinstance(game, dict):
            return None
        value = _first_value(
            game, "concurrentContainerLimit", "concurrent_container_limit",
            "containerCountLimit", "container_count_limit", "containerLimit",
        )
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    def _normalise_files(self, raw: Any) -> tuple[list[str], list[dict[str, Any]]]:
        if raw is None:
            return [], []
        values = raw if isinstance(raw, list) else [raw]
        urls: list[str] = []
        descriptors: list[dict[str, Any]] = []
        for value in values:
            if isinstance(value, str):
                urls.append(value)
                descriptors.append({"url": value})
                continue
            if not isinstance(value, dict):
                continue
            descriptor = {
                key: value[key]
                for key in ("url", "downloadUrl", "href", "folder", "file", "name")
                if value.get(key) is not None
            }
            url = descriptor.get("url") or descriptor.get("downloadUrl") or descriptor.get("href")
            if url:
                urls.append(str(url))
            descriptors.append(descriptor)
        return urls, descriptors

    def _normalise(self, raw: dict[str, Any]) -> PlatformChallenge | None:
        external = _first_value(raw, self.mapping.id_field, "id", "challengeId", "challenge_id")
        title = _first_value(raw, self.mapping.title_field, "name", "title")
        if external is None or title is None:
            return None
        category_raw = str(_first_value(raw, self.mapping.category_field, "category", "type", default="misc"))
        category = self.mapping.category_map.get(category_raw, category_raw).strip().lower()
        if category not in PLATFORM_CATEGORIES:
            category = "misc"
        files, descriptors = self._normalise_files(
            _first_value(raw, self.mapping.attachments_field, "files", "attachments", default=[])
        )
        level_raw = _mapping_value(raw, self.mapping.level_field, self.mapping.level) if self.mapping.level_field else self.mapping.level
        try:
            level = max(1, int(level_raw))
        except (TypeError, ValueError):
            level = self.mapping.level
        track = _mapping_value(raw, self.mapping.track_id_field, self.mapping.track_id) if self.mapping.track_id_field else self.mapping.track_id
        solved = _as_bool(_first_value(raw, self.mapping.solved_field, "solved", "isSolved", "isCompleted", default=False))
        container_type = _gz_container_type(raw)
        solve_count = _gz_solve_count(raw)
        platform_data: dict[str, Any] = {
            "level": level, "track_id": str(track or ""), "files": descriptors,
        }
        if container_type is not None:
            platform_data["container_type"] = container_type
        if solve_count is not None:
            platform_data["solve_count"] = solve_count
        return PlatformChallenge(
            external_id=str(external),
            title=str(title),
            category=category,
            description=str(_first_value(raw, self.mapping.description_field, "description", "content", default="") or ""),
            attachment_urls=[str(value) for value in files],
            remote=container_type is not None,
            solved=solved,
            hints=[],
            platform_data=platform_data,
        )

    def fetch_challenges(self) -> list[PlatformChallenge]:
        result: list[PlatformChallenge] = []
        seen: set[str] = set()
        refreshed: dict[str, PlatformChallenge] = {}
        raw_challenges = self.client.list_challenges(self.game_id)
        if self.mapping.max_challenges:
            raw_challenges = raw_challenges[: self.mapping.max_challenges]
        for raw in raw_challenges:
            enriched = dict(raw)
            getter = getattr(self.client, "get_challenge", None)
            if callable(getter):
                try:
                    challenge_id = _first_value(raw, "id", "challengeId", "challenge_id")
                    detail = getter(self.game_id, int(challenge_id))
                except (GZCTFError, requests.RequestException, ValueError, TypeError):
                    detail = None
                if isinstance(detail, dict):
                    enriched.update({key: value for key, value in detail.items() if value is not None})
                    context = detail.get("context")
                    if (
                        isinstance(context, dict)
                        and context.get("url")
                        and not any(enriched.get(key) for key in ("files", "attachments"))
                    ):
                        enriched["files"] = [{"url": str(context["url"])}]
            challenge = self._normalise(enriched)
            if challenge is None:
                continue
            if challenge.external_id in seen:
                raise GZCTFError(f"duplicate GZCTF challenge id: {challenge.external_id}")
            seen.add(challenge.external_id)
            refreshed[challenge.external_id] = challenge
            result.append(challenge)
        self._by_id = refreshed
        return result

    def fetch_public_scoreboard(self, *, limit: int | None = None) -> list[PlatformChallenge]:
        """Normalize public scoreboard rows using the normal GZCTF mapping.

        This method is discovery-only. It intentionally does not populate
        participant challenge state or enable submit/attachment operations.
        """

        payload = self.client.get_public_scoreboard(self.game_id)
        result: list[PlatformChallenge] = []
        seen: set[str] = set()
        for category, values in payload["challenges"].items():
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                raw = {**value, "category": value.get("category", category)}
                challenge = self._normalise(raw)
                if challenge is None or challenge.external_id in seen:
                    continue
                seen.add(challenge.external_id)
                result.append(challenge)
        result.sort(key=lambda item: (item.category, item.external_id))
        if limit is not None:
            if not 1 <= int(limit) <= 10:
                raise ValueError("public scoreboard limit must be between 1 and 10")
            result = result[: int(limit)]
        return result

    def _challenge(self, external_id: str) -> PlatformChallenge:
        if external_id not in self._by_id:
            self.fetch_challenges()
        challenge = self._by_id.get(str(external_id))
        if challenge is None:
            raise GZCTFError(f"unknown GZCTF challenge: {external_id}")
        return challenge

    def preflight(self) -> list[PlatformChallenge]:
        self._verify_scope()
        self.client.get_game(self.game_id)
        return self.fetch_challenges()

    def challenges(self) -> list[PlatformChallenge]:
        self.fetch_challenges()
        challenges = list(self._by_id.values())
        for challenge in challenges:
            try:
                state = self.client.get_challenge(self.game_id, int(challenge.external_id))
            except (GZCTFError, requests.RequestException, ValueError):
                continue
            challenge.solved = _as_bool(_first_value(state, "solved", "isSolved", "isCompleted"))
            challenge.platform_data = {
                **challenge.platform_data,
                "live_state": str(_first_value(state, "status", "state", default="")),
            }
        return [item.model_copy(deep=True) for item in self._by_id.values()]

    def download_attachments(self, challenge: PlatformChallenge, dest_dir: str | Path) -> list[Path]:
        destination = Path(dest_dir)
        destination.mkdir(parents=True, exist_ok=True)
        descriptors = challenge.platform_data.get("files", [])
        if not isinstance(descriptors, list):
            descriptors = []
        downloaded: list[Path] = []
        for index, descriptor in enumerate(descriptors, start=1):
            if not isinstance(descriptor, dict):
                continue
            url = descriptor.get("url") or descriptor.get("downloadUrl") or descriptor.get("href")
            params: dict[str, Any] | None = None
            if not url and descriptor.get("file"):
                url = f"/api/Game/{self.game_id}/Challenges/{challenge.external_id}/Files"
                params = {"folder": descriptor.get("folder", "static"), "file": descriptor["file"]}
            if not url:
                continue
            if not getattr(self.client, "logged_in", True):
                self.client.login()
            absolute = str(url) if str(url).startswith(("http://", "https://")) else urljoin(self.client.base_url + "/", str(url).lstrip("/"))
            parsed = urlsplit(absolute)
            base = urlsplit(self.client.base_url)
            allowed_origins = {base.netloc.lower()}
            attachment_base = urlsplit(self.mapping.attachment_base_url)
            if attachment_base.netloc:
                allowed_origins.add(attachment_base.netloc.lower())
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in allowed_origins:
                raise ValueError("GZCTF attachment URL must stay on the configured platform origin")
            source = Path(unquote(parsed.path)).name or str(descriptor.get("name") or f"attachment-{index}")
            target = destination / numbered_filename(
                safe_stem(Path(source).stem, fallback=f"attachment-{index}"),
                Path(source).suffix[:20] or ".bin",
                [path.name for path in destination.iterdir()],
                fallback=f"attachment-{index}",
            )
            response = self.client.session.get(absolute, params=params, timeout=self.client.timeout, stream=True)
            if response.status_code >= 400:
                response.raise_for_status()
            written = 0
            try:
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if self.max_attachment_bytes is not None and written > self.max_attachment_bytes:
                            raise ValueError(f"attachment exceeds {self.max_attachment_bytes} byte limit: {absolute}")
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

    def submit(self, external_id: str, flag: str) -> dict[str, Any]:
        challenge = self._challenge(external_id)
        data = challenge.platform_data
        level = int(data.get("level", self.mapping.level))
        track_id = str(data.get("track_id") or "") or None
        try:
            result = self.client.submit_level(
                self.game_id, int(external_id), flag, level=level, track_id=track_id
            )
        except GZCTFLoginError:
            return {"verdict": "auth_required"}
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", 0)
            if code == 429:
                headers = getattr(exc.response, "headers", {}) or {}
                try:
                    retry_after = max(1, int(headers.get("Retry-After", "60")))
                except (TypeError, ValueError):
                    retry_after = 60
                return {"verdict": "rate_limited", "retry_after": retry_after}
            return {"verdict": "auth_required" if code in (401, 403) else "unknown"}
        except GZCTFPreflightError as exc:
            reason = str(exc)
            if any(token in reason.lower() for token in ("cooldown", "wait", "rate limit")):
                return {"verdict": "rate_limited", "retry_after": 60, "reason": reason}
            return {"verdict": "rejected", "reason": reason}
        return _normalise_gz_verdict(result)

    def query(self, external_id: str, submission_id: str) -> dict[str, Any]:
        try:
            result = self.client.get_submission_status(
                self.game_id, int(external_id), int(submission_id)
            )
        except GZCTFLoginError:
            return {"verdict": "auth_required", "submission_id": submission_id}
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", 0)
            if code == 429:
                headers = getattr(exc.response, "headers", {}) or {}
                try:
                    retry_after = max(1, int(headers.get("Retry-After", "60")))
                except (TypeError, ValueError):
                    retry_after = 60
                return {"verdict": "rate_limited", "retry_after": retry_after, "submission_id": submission_id}
            return {"verdict": "auth_required" if code in (401, 403) else "unknown", "submission_id": submission_id}
        return _normalise_gz_verdict(result, submission_id=submission_id)

    def _container_context(self, external_id: str) -> dict[str, Any]:
        """Read the live container context for one challenge.

        ``get_container`` is the dedicated reader, but it is not part of the
        capability check, so a client without it must still work: the same
        information lives in the challenge detail's ``context``.
        """
        reader = getattr(self.client, "get_container", None)
        if not callable(reader):
            reader = getattr(self.client, "get_challenge", None)
        if not callable(reader):
            return {}
        try:
            payload = reader(self.game_id, int(external_id))
        except (GZCTFError, requests.RequestException, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def instances(self) -> list[dict[str, Any]]:
        """Report the containers this team currently holds.

        Read from each known container challenge's live context, which is the
        only place GZCTF exposes an existing instance. Used at startup to
        reconcile against the local table so an orphan is not left running.
        """
        if not self.supports_instances:
            return []
        found: list[dict[str, Any]] = []
        for challenge in self._by_id.values():
            if not challenge.remote:
                continue
            instance = _gz_instance_view(self._container_context(challenge.external_id))
            if instance.get("state") == "ready":
                found.append({"external_id": challenge.external_id, **instance})
        return found

    def start_instance(self, external_id: str) -> dict[str, Any]:
        challenge = self._challenge(external_id)
        if not self.supports_instances:
            raise ValueError("GZCTF client does not expose the container lifecycle")
        if not challenge.remote:
            raise ValueError(
                f"GZCTF challenge {external_id} is not a container challenge"
            )
        try:
            created = self.client.create_container(self.game_id, int(external_id))
        except requests.HTTPError as exc:
            raise _gz_container_error(exc, external_id) from exc
        instance = _gz_instance_view(created)
        if instance.get("state") != "ready":
            instance = _gz_instance_view(self._container_context(external_id)) or instance
        return instance

    def stop_instance(self, external_id: str) -> None:
        if not self.supports_instances:
            return
        try:
            self.client.delete_container(self.game_id, int(external_id))
        except requests.HTTPError as exc:
            raise _gz_container_error(exc, external_id) from exc
        return

    def renew_instance(self, external_id: str) -> dict[str, Any]:
        if not self.supports_instances:
            raise ValueError("GZCTF client does not expose the container lifecycle")
        try:
            extended = self.client.extend_container(self.game_id, int(external_id))
        except requests.HTTPError as exc:
            raise _gz_container_error(exc, external_id) from exc
        instance = _gz_instance_view(extended)
        if not instance.get("expires_at"):
            refreshed = _gz_instance_view(self._container_context(external_id))
            if refreshed.get("expires_at"):
                instance = refreshed
        return instance

    def rebuild_instance(self, external_id: str) -> dict[str, Any]:
        """Destroy and recreate a container whose lease could not be extended.

        GZCTF refuses to extend a container that already expired or was taken
        over, and there is no in-place repair. Deleting first keeps the team
        within its container quota, which a bare create would not.
        """
        if not self.supports_instances:
            raise ValueError("GZCTF client does not expose the container lifecycle")
        try:
            self.client.delete_container(self.game_id, int(external_id))
        except (GZCTFError, requests.RequestException, ValueError, TypeError):
            pass
        return self.start_instance(external_id)
