from __future__ import annotations

from pathlib import Path

import pytest

from backend.platform.adapter import HttpJsonAdapter
from backend.platform.mapping import FieldMapping


class _Response:
    def __init__(self, payload=None, *, content=b"", status_code=200, headers=None):
        self._payload = payload
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self._content


def test_http_json_cursor_pagination_is_bounded_and_deduplicated_by_caller():
    calls = []
    pages = {
        "https://ctf.example/challenges": {
            "data": [{"id": "one", "name": "One"}],
            "paging": {"next": "/challenges?page=2"},
        },
        "https://ctf.example/challenges?page=2": {
            "data": [{"id": "two", "name": "Two"}],
            "paging": {"next": None},
        },
    }

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(pages[url])

    adapter = HttpJsonAdapter(
        FieldMapping(
            list_url="https://ctf.example/challenges",
            pagination_path="paging.next",
        ),
        request_get=get,
    )
    result = adapter.fetch_challenges()
    assert [item.external_id for item in result] == ["one", "two"]
    assert len(calls) == 2
    assert "params" not in calls[0][1]


def test_http_json_pagination_rejects_non_json_and_unbounded_cursor():
    adapter = HttpJsonAdapter(
        FieldMapping(list_url="https://ctf.example/challenges", pagination_path="next", max_pages=1),
        request_get=lambda *_args, **_kwargs: _Response({"data": [], "next": "/again"}),
    )
    with pytest.raises(ValueError, match="max_pages"):
        adapter.fetch_challenges()

    broken = HttpJsonAdapter(
        FieldMapping(list_url="https://ctf.example/challenges"),
        request_get=lambda *_args, **_kwargs: _Response(ValueError("html")),
    )
    with pytest.raises(ValueError, match="JSON"):
        broken.fetch_challenges()


def test_http_json_attachment_rejects_successful_authentication_html(tmp_path: Path):
    adapter = HttpJsonAdapter(
        FieldMapping(
            list_url="https://ctf.example/challenges",
            headers={"Authorization": "Bearer redacted"},
        ),
        request_get=lambda *_args, **kwargs: _Response(
            content=b"<!doctype html><html>login</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        ),
    )
    challenge = adapter._normalize({"id": "1", "name": "one", "files": ["/secret.bin"]})
    with pytest.raises(ValueError, match="HTML/authentication"):
        adapter.download_attachments(challenge, tmp_path)
    assert list(tmp_path.iterdir()) == []
