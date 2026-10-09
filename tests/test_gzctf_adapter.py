from __future__ import annotations

from pathlib import Path

import pytest

from backend.platform.gzctf import GZCTFAdapter, GZCTFPreflightError
from backend.platform.mapping import FieldMapping
from backend.competition.platform import CompetitionPlatform
from backend.ops.models import PlatformWorkflowSpec


class _Response:
    def __init__(self, body: bytes = b"demo", status_code: int = 200):
        self.body = body
        self.status_code = status_code

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        return None


class _Session:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        del kwargs
        self.urls.append(url)
        return _Response()


class _Client:
    base_url = "https://gz.example"
    timeout = 3

    def __init__(self):
        self.session = _Session()
        self.submissions: list[tuple[int, int, str, str | None]] = []
        self.statuses = {"pending": {"id": 91, "status": "Pending"}}

    def list_challenges(self, game_id):
        assert game_id == 7
        return [
            {
                "id": 11,
                "name": "Web level",
                "category": "Web",
                "description": "solve it",
                "level": 2,
                "trackId": "blue",
                "files": [{"url": "/files/web.zip", "name": "web.zip"}],
            },
            {
                "id": 12,
                "name": "Locked",
                "category": "pwn",
                "tracks": [],
                "solved": False,
            },
        ]

    def get_profile(self):
        return {"userName": "fixture"}

    def get_public_scoreboard(self, game_id):
        assert game_id == 7
        return {
            "challenges": {
                "Web": [{"id": 11, "title": "Web level", "score": 100}],
                "Crypto": [{"id": 13, "title": "RSA", "score": 200}],
            }
        }

    def get_game(self, game_id):
        assert game_id == 7
        return {"id": game_id}

    def get_challenge(self, game_id, challenge_id):
        del game_id
        if challenge_id == 12:
            return {"isSolved": False, "status": "locked"}
        return {"isSolved": False, "status": "open"}

    def submit_level(self, game_id, challenge_id, flag, *, level, track_id):
        self.submissions.append((game_id, challenge_id, flag, track_id))
        assert level == 2
        if flag == "locked":
            raise GZCTFPreflightError("challenge is currently locked")
        if flag == "pending":
            return self.statuses["pending"]
        return {"id": 92, "solved": flag == "correct"}

    def get_submission_status(self, game_id, challenge_id, submission_id):
        assert (game_id, challenge_id, submission_id) == (7, 11, 91)
        return {"id": submission_id, "status": "Correct"}


def _adapter(client=None):
    mapping = FieldMapping(
        platform="gzctf",
        game_id=7,
        level=1,
        level_field="level",
        track_id_field="trackId",
    )
    return GZCTFAdapter(client or _Client(), mapping, max_attachment_bytes=1024)


def test_gzctf_adapter_normalizes_track_level_and_refreshes_live_state():
    client = _Client()
    adapter = _adapter(client)

    assert [item.external_id for item in adapter.preflight()] == ["11", "12"]
    refreshed = adapter.challenges()
    first = next(item for item in refreshed if item.external_id == "11")
    assert first.category == "web"
    assert first.platform_data["level"] == 2
    assert first.platform_data["track_id"] == "blue"
    assert first.platform_data["files"][0]["name"] == "web.zip"


def test_gzctf_adapter_normalizes_public_scoreboard_without_login():
    adapter = _adapter(_Client())
    rows = adapter.fetch_public_scoreboard(limit=2)
    assert [(row.external_id, row.title, row.category) for row in rows] == [
        ("13", "RSA", "crypto"),
        ("11", "Web level", "web"),
    ]


def test_gzctf_adapter_submission_and_query_verdict_contract():
    client = _Client()
    adapter = _adapter(client)
    adapter.fetch_challenges()

    assert adapter.submit("11", "wrong")["verdict"] == "wrong"
    pending = adapter.submit("11", "pending")
    assert pending == {"verdict": "pending", "submission_id": "91"}
    assert adapter.query("11", "91") == {"verdict": "correct", "submission_id": "91"}
    assert adapter.submit("11", "correct")["verdict"] == "correct"
    assert client.submissions[-1] == (7, 11, "correct", "blue")
    rejected = adapter.submit("11", "locked")
    assert rejected["verdict"] == "rejected"
    assert "locked" in rejected["reason"]


def test_gzctf_adapter_handles_bare_answer_result_verdicts():
    class _BareClient(_Client):
        def submit_level(self, game_id, challenge_id, flag, *, level, track_id):
            if flag == "bare":
                return "WrongAnswer"
            return {"id": 66896}

        def get_submission_status(self, game_id, challenge_id, submission_id):
            return {
                66896: "WrongAnswer",
                1: "CorrectAnswer",
                2: "CheatDetected",
                3: "Accepted",
                4: "Pending",
            }[int(submission_id)]

    adapter = _adapter(_BareClient())
    adapter.fetch_challenges()

    assert adapter.submit("11", "bare") == {"verdict": "wrong", "submission_id": None}
    submitted = adapter.submit("11", "candidate")
    assert submitted == {"verdict": "unknown", "submission_id": "66896"}
    assert adapter.query("11", "66896") == {"verdict": "wrong", "submission_id": "66896"}
    assert adapter.query("11", "1") == {"verdict": "correct", "submission_id": "1"}
    assert adapter.query("11", "2") == {"verdict": "rejected", "submission_id": "2"}
    assert adapter.query("11", "3") == {"verdict": "correct", "submission_id": "3"}
    assert adapter.query("11", "4") == {"verdict": "pending", "submission_id": "4"}


def test_gzctf_adapter_downloads_and_enforces_attachment_limit(tmp_path: Path):
    client = _Client()
    adapter = _adapter(client)
    challenge = adapter.fetch_challenges()[0]
    downloaded = adapter.download_attachments(challenge, tmp_path)
    assert downloaded[0].name == "web.zip"
    assert downloaded[0].read_bytes() == b"demo"
    assert client.session.urls == ["https://gz.example/files/web.zip"]

    limited = GZCTFAdapter(client, adapter.mapping, max_attachment_bytes=2)
    with pytest.raises(ValueError, match="exceeds"):
        limited.download_attachments(challenge, tmp_path / "limited")


def test_gzctf_mapping_requires_game_id():
    with pytest.raises(ValueError, match="game_id"):
        FieldMapping(platform="gzctf")


def test_competition_platform_uses_gzctf_native_contract():
    client = _Client()
    adapter = _adapter(client)
    spec = PlatformWorkflowSpec.model_validate(
        {
            "name": "GZ fixture",
            "competition_id": "7",
            "team_id": "team",
            "challenges": {"platform": "gzctf", "game_id": 7},
        }
    )

    class _Store:
        @staticmethod
        def workflow_secrets(_workflow_id):
            return {}

    class _Ops:
        store = _Store()

        @staticmethod
        def _adapter(_workflow_id, _spec):
            return adapter

    platform = CompetitionPlatform(_Ops(), "workflow", spec)
    assert platform.preflight()[0].external_id == "11"
    assert platform.submit("11", "correct")["verdict"] == "correct"
    assert platform.query("11", "91")["verdict"] == "correct"
    assert platform.instances() == []
