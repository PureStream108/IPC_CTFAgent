from __future__ import annotations

from backend.agent import job_digest


def test_parse_curator_response_rejects_non_dict_json(monkeypatch):
    monkeypatch.setattr(job_digest.json, "loads", lambda _text: [1, 2])
    assert job_digest.parse_curator_response("{not a dict}") is None
