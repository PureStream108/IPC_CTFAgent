from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.auth.session_store import ActiveSessionStore

AUTH_FILE_ENV = "IPC_AUTH_FILE"
SESSION_COOKIE_NAME = "ipc_session"

_AUTH_VERSION = 1
_SESSION_VERSION = 1
_PASSWORD_ALGORITHM = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 600_000
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 1024
_DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
_MAX_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_LOCKOUT_SECONDS = 30
_MAX_LOGIN_CLIENTS = 4096


class AuthConfigurationError(RuntimeError):
    """The persistent authentication configuration is invalid."""


class AlreadyConfiguredError(RuntimeError):
    """Initial administrator setup has already completed."""


@dataclass(slots=True)
class _LoginAttempts:
    failures: int = 0
    locked_until: float = 0


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("missing encoded value")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _session_ttl_from_env() -> int:
    raw = os.environ.get("IPC_SESSION_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_SESSION_TTL_SECONDS
    return min(max(value, 300), _MAX_SESSION_TTL_SECONDS)


def _auth_path(root: Path) -> Path:
    override = os.environ.get(AUTH_FILE_ENV)
    if not override:
        return root / "data" / "auth.json"
    path = Path(override).expanduser()
    return path if path.is_absolute() else root / path


class AuthManager:
    """Persist an administrator password verifier and sign browser sessions."""

    def __init__(
        self,
        root: str | Path,
        *,
        auth_file: str | Path | None = None,
        session_ttl_seconds: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root_path = Path(root)
        self.auth_file = Path(auth_file) if auth_file is not None else _auth_path(root_path)
        self.sessions_file = self.auth_file.with_name(f"{self.auth_file.stem}_sessions.db")
        self._sessions = ActiveSessionStore(self.sessions_file)
        self.session_ttl_seconds = min(
            max(session_ttl_seconds or _session_ttl_from_env(), 300),
            _MAX_SESSION_TTL_SECONDS,
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._login_attempts: OrderedDict[str, _LoginAttempts] = OrderedDict()
        self._config: dict[str, Any] | None = None
        self._configuration_error: str | None = None
        self._load_if_present()

    @property
    def setup_required(self) -> bool:
        return not self.auth_file.exists()

    @property
    def configuration_error(self) -> str | None:
        if self._config is None and self.auth_file.exists() and self._configuration_error is None:
            with self._lock:
                if self._config is None and self._configuration_error is None:
                    self._load_if_present()
        return self._configuration_error

    def setup(self, password: str) -> None:
        self._validate_new_password(password)
        with self._lock:
            if self.auth_file.exists():
                self._load_if_present()
                raise AlreadyConfiguredError("administrator setup is already complete")

            salt = secrets.token_bytes(16)
            password_digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt,
                _PASSWORD_ITERATIONS,
            )
            config = {
                "version": _AUTH_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "password": {
                    "algorithm": _PASSWORD_ALGORITHM,
                    "iterations": _PASSWORD_ITERATIONS,
                    "salt": _encode(salt),
                    "digest": _encode(password_digest),
                },
                "session_secret": _encode(secrets.token_bytes(32)),
            }
            self._write_exclusive(config)
            self._config = config
            self._configuration_error = None

    def verify_password(self, password: str) -> bool:
        config = self._require_config()
        password_config = config["password"]
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _decode(password_config["salt"]),
            password_config["iterations"],
        )
        return hmac.compare_digest(candidate, _decode(password_config["digest"]))

    def login_retry_after(self, client_key: str) -> int:
        with self._lock:
            attempts = self._login_attempts.get(client_key)
            if attempts is None:
                return 0
            now = self._clock()
            if attempts.locked_until <= now:
                if attempts.locked_until:
                    self._login_attempts.pop(client_key, None)
                return 0
            self._login_attempts.move_to_end(client_key)
            return max(1, int(attempts.locked_until - now + 0.999))

    def record_login_failure(self, client_key: str) -> None:
        with self._lock:
            attempts = self._login_attempts.get(client_key)
            now = self._clock()
            if attempts is None or (attempts.locked_until and attempts.locked_until <= now):
                attempts = _LoginAttempts()
                self._login_attempts[client_key] = attempts
            attempts.failures += 1
            if attempts.failures >= _LOGIN_FAILURE_LIMIT:
                attempts.locked_until = now + _LOGIN_LOCKOUT_SECONDS
            self._login_attempts.move_to_end(client_key)
            while len(self._login_attempts) > _MAX_LOGIN_CLIENTS:
                self._login_attempts.popitem(last=False)

    def clear_login_failures(self, client_key: str) -> None:
        with self._lock:
            self._login_attempts.pop(client_key, None)

    def create_session(self) -> str:
        config = self._require_config()
        issued_at = int(self._clock())
        payload = {
            "exp": issued_at + self.session_ttl_seconds,
            "iat": issued_at,
            "sid": _encode(secrets.token_bytes(16)),
            "v": _SESSION_VERSION,
        }
        encoded_payload = _encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            _decode(config["session_secret"]),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        self._sessions.register(payload["sid"], payload["exp"], issued_at)
        return f"{encoded_payload}.{_encode(signature)}"

    def verify_session(self, token: str | None) -> bool:
        if not token or self.setup_required or self._configuration_error:
            return False
        try:
            payload = self._verified_session_payload(token)
            if payload is None:
                return False
            issued_at = payload["iat"]
            expires_at = payload["exp"]
            now = int(self._clock())
            if issued_at > now + 60 or expires_at <= now:
                return False
            return self._sessions.is_active(payload["sid"], expires_at, now)
        except (AuthConfigurationError, OSError, TypeError, ValueError):
            return False

    def revoke_session(self, token: str | None) -> bool:
        if not token or self.setup_required or self._configuration_error:
            return False
        try:
            payload = self._verified_session_payload(token)
            if payload is None:
                return False
            return self._sessions.revoke(payload["sid"], int(self._clock()))
        except (AuthConfigurationError, OSError, TypeError, ValueError):
            return False

    def _verified_session_payload(self, token: str) -> dict[str, Any] | None:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            config = self._require_config()
            expected_signature = hmac.new(
                _decode(config["session_secret"]),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected_signature, _decode(encoded_signature)):
                return None
            payload = json.loads(_decode(encoded_payload))
            if not isinstance(payload, dict):
                return None
            issued_at = payload.get("iat")
            expires_at = payload.get("exp")
            session_id = payload.get("sid")
            if (
                payload.get("v") != _SESSION_VERSION
                or not isinstance(session_id, str)
                or len(_decode(session_id)) != 16
                or type(issued_at) is not int
                or type(expires_at) is not int
                or expires_at - issued_at != self.session_ttl_seconds
            ):
                return None
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _load_if_present(self) -> None:
        if not self.auth_file.exists():
            self._config = None
            self._configuration_error = None
            return
        try:
            raw = json.loads(self.auth_file.read_text(encoding="utf-8"))
            self._validate_config(raw)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self._config = None
            self._configuration_error = f"invalid authentication configuration: {exc}"
            return
        self._config = raw
        self._configuration_error = None
        self._restrict_permissions()

    def _require_config(self) -> dict[str, Any]:
        if self._config is None and self.auth_file.exists() and self._configuration_error is None:
            with self._lock:
                if self._config is None and self._configuration_error is None:
                    self._load_if_present()
        if self._configuration_error:
            raise AuthConfigurationError(self._configuration_error)
        if self._config is None:
            raise AuthConfigurationError("administrator setup is required")
        return self._config

    def _write_exclusive(self, config: dict[str, Any]) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.auth_file.parent, 0o700)
        except OSError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        temporary_file = self.auth_file.with_name(
            f".{self.auth_file.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        try:
            descriptor = os.open(temporary_file, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(config, output, separators=(",", ":"), sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary_file, self.auth_file)
            except FileExistsError as exc:
                self._load_if_present()
                raise AlreadyConfiguredError("administrator setup is already complete") from exc
        finally:
            temporary_file.unlink(missing_ok=True)
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        try:
            os.chmod(self.auth_file, 0o600)
        except OSError:
            pass

    @staticmethod
    def _validate_new_password(password: str) -> None:
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if len(password) < _MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must contain at least {_MIN_PASSWORD_LENGTH} characters")
        if len(password) > _MAX_PASSWORD_LENGTH:
            raise ValueError(f"password must contain at most {_MAX_PASSWORD_LENGTH} characters")
        if not password.strip():
            raise ValueError("password cannot contain only whitespace")

    @staticmethod
    def _validate_config(config: Any) -> None:
        if not isinstance(config, dict) or config.get("version") != _AUTH_VERSION:
            raise ValueError("unsupported auth configuration version")
        password = config["password"]
        if not isinstance(password, dict) or password.get("algorithm") != _PASSWORD_ALGORITHM:
            raise ValueError("unsupported password algorithm")
        iterations = password["iterations"]
        if not isinstance(iterations, int) or iterations < 100_000:
            raise ValueError("unsafe password iteration count")
        if len(_decode(password["salt"])) < 16:
            raise ValueError("password salt is too short")
        if len(_decode(password["digest"])) != hashlib.sha256().digest_size:
            raise ValueError("invalid password digest")
        if len(_decode(config["session_secret"])) < 32:
            raise ValueError("session secret is too short")
