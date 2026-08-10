from __future__ import annotations

import json
import os
import stat

from fastapi.testclient import TestClient

from backend.auth import SESSION_COOKIE_NAME, AuthManager
from backend.server.app import create_app

PASSWORD = "correct horse battery staple"


def test_first_run_opens_api_and_compat_setup_sets_secure_session(tmp_path):
    app = create_app(root=tmp_path)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/ipc.png").status_code == 200
        assert client.get("/health").json() == {"status": "ok", "setup_required": False}
        assert client.get("/projects").status_code == 200
        assert client.get("/auth/status").json() == {
            "setup_required": False,
            "authenticated": True,
            "username": None,
        }

        weak = client.post("/auth/setup", json={"password": "short"})
        assert weak.status_code == 422

        setup = client.post("/auth/setup", json={"password": PASSWORD})
        assert setup.status_code == 201
        assert setup.json()["authenticated"] is True
        set_cookie = setup.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "path=/" in set_cookie
        assert client.get("/projects").status_code == 200
        assert client.post("/auth/setup", json={"password": PASSWORD}).status_code == 409

    auth_file = tmp_path / "data" / "auth.json"
    raw = auth_file.read_text(encoding="utf-8")
    config = json.loads(raw)
    assert PASSWORD not in raw
    assert config["password"]["algorithm"] == "pbkdf2_sha256"
    assert config["session_secret"]
    if os.name != "nt":
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_compat_login_logout_does_not_gate_trusted_api(tmp_path):
    first_app = create_app(root=tmp_path)
    with TestClient(first_app) as client:
        assert client.post("/auth/setup", json={"password": PASSWORD}).status_code == 201

    second_app = create_app(root=tmp_path)
    with TestClient(second_app) as client:
        assert client.get("/projects").status_code == 200
        assert client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong"},
        ).status_code == 401
        assert client.post(
            "/auth/login",
            json={"username": "someone-else", "password": PASSWORD},
        ).status_code == 401

        login = client.post("/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert client.get("/auth/status").json()["authenticated"] is True
        assert client.get("/projects").status_code == 200

        assert client.post("/auth/logout").status_code == 204
        assert client.get("/projects").status_code == 200
        client.cookies.set(SESSION_COOKIE_NAME, old_cookie)
        assert client.get("/projects").status_code == 200


def test_tampered_and_expired_sessions_are_rejected(tmp_path):
    now = [1_000_000.0]
    manager = AuthManager(
        tmp_path,
        session_ttl_seconds=300,
        clock=lambda: now[0],
    )
    manager.setup(PASSWORD)
    token = manager.create_session()

    assert manager.verify_session(token) is True
    encoded_payload, signature = token.split(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    assert manager.verify_session(f"{encoded_payload}.{replacement}{signature[1:]}") is False
    assert manager.verify_session("not-a-session") is False

    now[0] += 301
    assert manager.verify_session(token) is False


def test_active_sessions_persist_and_revocation_is_shared(tmp_path):
    first_manager = AuthManager(tmp_path)
    first_manager.setup(PASSWORD)
    token = first_manager.create_session()

    second_manager = AuthManager(tmp_path)
    assert second_manager.verify_session(token) is True
    assert second_manager.revoke_session(token) is True
    assert first_manager.verify_session(token) is False


def test_invalid_compat_auth_file_does_not_block_trusted_api(tmp_path):
    auth_file = tmp_path / "data" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")

    app = create_app(root=tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/auth/status").json() == {
            "setup_required": False,
            "authenticated": True,
            "username": None,
        }
        assert client.post("/auth/setup", json={"password": PASSWORD}).status_code == 503
        assert client.get("/projects").status_code == 200


def test_https_login_marks_cookie_secure(tmp_path):
    app = create_app(root=tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/auth/setup", json={"password": PASSWORD})
        assert response.status_code == 201
        assert "secure" in response.headers["set-cookie"].lower()


def test_repeated_login_failures_are_rate_limited(tmp_path):
    app = create_app(root=tmp_path)
    with TestClient(app) as client:
        assert client.post("/auth/setup", json={"password": PASSWORD}).status_code == 201
        client.cookies.clear()
        for _ in range(5):
            response = client.post("/auth/login", json={"password": "incorrect"})
            assert response.status_code == 401

        blocked = client.post("/auth/login", json={"password": PASSWORD})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0


def test_auth_file_override_is_relative_to_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IPC_AUTH_FILE", "private/credentials.json")
    manager = AuthManager(tmp_path)
    manager.setup(PASSWORD)

    assert manager.auth_file == tmp_path / "private" / "credentials.json"
    assert manager.auth_file.exists()
