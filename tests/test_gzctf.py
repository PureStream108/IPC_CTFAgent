from __future__ import annotations

import pytest

from backend.platform.gzctf import (
    GZCTFClient,
    GZCTFLoginError,
    GZCTFPreflightError,
    validate_submission,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/api/Account/LogIn"):
            if kwargs["json"]["password"] != "correct":
                return FakeResponse(
                    401,
                    {"title": "Wrong username or password", "status": 401},
                )
            return FakeResponse(200, None)
        if url.endswith("/Challenges/7"):
            return FakeResponse(200, {"id": 1234, "isTrackComplete": True})
        return FakeResponse(200, {"ok": True})

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url.endswith("/api/Account/Profile"):
            return FakeResponse(200, {"userName": "tester"})
        return FakeResponse(200, {"ok": True})

    def close(self):
        self.closed = True


def test_gzctf_login_failure_raises(monkeypatch):
    monkeypatch.setattr("backend.platform.gzctf.requests.Session", FakeSession)
    client = GZCTFClient(
        base_url="https://ctf.test",
        username="user",
        password="bad",
    )
    with pytest.raises(GZCTFLoginError, match="Wrong username"):
        client.login()
    client.close()


def test_gzctf_authenticated_flow(monkeypatch):
    monkeypatch.setattr("backend.platform.gzctf.requests.Session", FakeSession)
    client = GZCTFClient(
        base_url="https://ctf.test",
        username="user",
        password="correct",
    )
    profile = client.get_profile()
    details = client.get_game_details(2)
    check = client.get_game_check(2)
    submission = client.submit_flag(
        2,
        7,
        "flag{demo}",
        level=2,
        track_id="track-1",
    )
    status = client.get_submission_status(2, 7, submission["id"])

    assert profile["userName"] == "tester"
    assert details == {"ok": True}
    assert check == {"ok": True}
    assert submission["id"] == 1234
    assert status == {"ok": True}
    urls = [call[1] for call in client.session.calls]
    assert any(url.endswith("/api/Account/LogIn") for url in urls)
    assert any(url.endswith("/api/Game/2/Check") for url in urls)
    assert any(url.endswith("/api/Game/2/Challenges/7") for url in urls)
    assert any(url.endswith("/api/Game/2/Challenges/7/Status/1234") for url in urls)
    submit_call = next(
        call
        for call in client.session.calls
        if call[1].endswith("/api/Game/2/Challenges/7")
    )
    assert submit_call[2]["json"] == {
        "flag": "flag{demo}",
        "level": 2,
        "trackId": "track-1",
    }
    client.close()


def _challenge_state(*, multi=True, current_level=1, cooldown=None):
    tracks = [
        {
            "trackId": "track-1",
            "isEnabled": True,
            "currentLevel": current_level,
            "levels": [
                {"level": 1, "status": "Unsolved"},
                {"level": 2, "status": "Locked"},
                {"level": 3, "status": "Locked"},
            ],
        }
    ]
    if multi:
        tracks.append(
            {
                "trackId": "track-2",
                "isEnabled": True,
                "currentLevel": current_level,
                "levels": [{"level": 1, "status": "Unsolved"}],
            }
        )
    return {"id": 143, "challengeLock": False, "tracks": tracks, "cooldown": cooldown or {}}


def test_gzctf_preflight_requires_track_and_strips_answer():
    with pytest.raises(GZCTFPreflightError, match="multiple enabled tracks"):
        validate_submission(
            _challenge_state(),
            level=1,
            answer="flag{demo}",
        )
    with pytest.raises(GZCTFPreflightError, match="answer must not be empty"):
        validate_submission(
            _challenge_state(multi=False),
            level=1,
            answer="   ",
        )
    decision = validate_submission(
        _challenge_state(),
        level=1,
        answer="  flag{demo}  ",
        track_id="track-1",
        evidence={
            "status": "verified",
            "answers": {"1": "flag{demo}"},
        },
    )
    assert decision["answer"] == "flag{demo}"
    assert decision["track_id"] == "track-1"


def test_gzctf_preflight_refuses_locked_and_cooldown_levels():
    with pytest.raises(GZCTFPreflightError, match="locked"):
        validate_submission(
            _challenge_state(multi=False),
            level=2,
            answer="flag{demo}",
        )
    with pytest.raises(GZCTFPreflightError, match="wrong-answer limit"):
        validate_submission(
            _challenge_state(
                multi=False,
                cooldown={"wrongCount": 10, "wrongAnswerLimit": 10},
            ),
            level=1,
            answer="flag{demo}",
        )


def test_gzctf_preflight_accepts_unlocked_status_object():
    challenge = _challenge_state(multi=False)
    challenge["challengeLock"] = {
        "isLocked": False,
        "unlocksAt": None,
        "triggerAnswer": None,
    }
    decision = validate_submission(
        challenge,
        level=1,
        answer="flag{demo}",
        evidence={
            "status": "verified",
            "answers": {"1": "flag{demo}"},
        },
    )
    assert decision["ok"] is True


def test_gzctf_preflight_refuses_locked_status_object():
    challenge = _challenge_state(multi=False)
    challenge["challengeLock"] = {"isLocked": True, "reason": "banned"}
    with pytest.raises(GZCTFPreflightError, match="currently locked"):
        validate_submission(
            challenge,
            level=1,
            answer="services/foo.c:1",
        )
