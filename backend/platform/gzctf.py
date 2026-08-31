from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import requests


class GZCTFError(RuntimeError):
    pass


class GZCTFLoginError(GZCTFError):
    pass


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
            # GZCTF exposes track metadata and per-level state as sibling
            # arrays.  Join them here so preflight sees one uniform shape.
            joined = dict(item)
            if not isinstance(joined.get("levels"), list):
                joined["levels"] = [
                    flag
                    for flag in flags
                    if isinstance(flag, dict) and _track_id(flag) == _track_id(item)
                ]
            result.append(joined)
        return result
    # Some GZCTF versions expose the per-track level state under `flags`.
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
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
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
        # Current GZCTF returns a status object such as
        # ``{"isLocked": false, "unlocksAt": null}``; treating the
        # object itself as truthy would incorrectly reject every preflight.
        challenge_lock = challenge_lock.get(
            "isLocked", challenge_lock.get("locked", challenge_lock.get("is_locked", False))
        )
    if bool(challenge_lock):
        raise GZCTFPreflightError("challenge is currently locked")

    tracks = _track_entries(challenge)
    enabled = [item for item in tracks if item.get("isEnabled", item.get("is_enabled", True))]
    if not enabled:
        raise GZCTFPreflightError("live challenge has no enabled track state")
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
        *,
        timeout: float = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("IPC_GZ_BASE_URL", "")).rstrip("/")
        self.username = username or os.getenv("IPC_GZ_USERNAME", "")
        self.password = password or os.getenv("IPC_GZ_PASSWORD", "")
        self.timeout = timeout
        self.session = requests.Session()
        self.logged_in = False

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _require_base_url(self) -> None:
        if not self.base_url:
            raise GZCTFError("GZCTF base_url is not configured; set IPC_GZ_BASE_URL")

    def login(self) -> None:
        self._require_base_url()
        if not self.username or not self.password:
            raise GZCTFLoginError("GZCTF username/password are not configured")
        response = self.session.post(
            self._url("/api/Account/LogIn"),
            json={"userName": self.username, "password": self.password},
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
        response = self.session.get(
            self._url(path), params=params, timeout=self.timeout
        )
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
        if response.status_code >= 400:
            # Keep the platform's validation reason in the exception.  A bare
            # ``raise_for_status`` only exposed ``400 Bad Request`` and made
            # it impossible to distinguish a contract mismatch from a wrong
            # answer without issuing another blind attempt.
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

    def get_game(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}")

    def get_game_details(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Details")

    def get_game_check(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Check")

    def get_game_participation(self, game_id: int) -> Any:
        return self._authorized_get(f"/api/game/{game_id}/participations")

    def get_scoreboard(self, game_id: int) -> dict[str, Any]:
        return self._authorized_get(f"/api/Game/{game_id}/Scoreboard")

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
        body: dict[str, Any] = {"flag": flag, "level": level}
        if track_id:
            body["trackId"] = track_id
        result = self._authorized_post(
            f"/api/Game/{game_id}/Challenges/{challenge_id}",
            body,
        )
        if not isinstance(result, dict):
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

    def __enter__(self) -> GZCTFClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
