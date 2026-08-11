from __future__ import annotations

import pytest

from backend.ops.models import PlatformWorkflowSpec, SecretHeader
from backend.ops.network import NetworkPolicyError, _read_bounded_content
from backend.ops.service import (
    OpsAgentService,
    _normalize_secrets,
    _replace_secret_values,
    _validate_workflow_secret_names,
)
from backend.platform.adapter import HttpJsonAdapter
from backend.platform.mapping import FieldMapping, PlatformChallenge


class FakeResponse:
    def __init__(self, *, payload=None, chunks=(), headers=None):
        self.payload = payload
        self.chunks = list(chunks)
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_platform_adapter_handles_nested_attachment_objects_and_closes_list_response():
    response = FakeResponse(
        payload={
            "data": [
                {
                    "ids": ["first"],
                    "name": "Nested files",
                    "files": [
                        {"downloadUrl": "files/a.txt", "name": "a.txt"},
                        {"href": "https://cdn.example.test/b.bin"},
                    ],
                }
            ]
        }
    )
    adapter = HttpJsonAdapter(
        FieldMapping(
            list_url="https://ctf.example.test/api/challenges",
            id_field="ids.0",
            attachment_base_url="https://ctf.example.test/assets/",
        ),
        request_get=lambda *args, **kwargs: response,
    )

    challenge = adapter.fetch_challenges()[0]

    assert challenge.external_id == "first"
    assert challenge.attachment_urls == [
        "https://ctf.example.test/assets/files/a.txt",
        "https://cdn.example.test/b.bin",
    ]
    assert response.closed is True


def test_platform_adapter_out_of_range_path_uses_missing_field_error():
    adapter = HttpJsonAdapter(
        FieldMapping(list_url="https://ctf.example.test/api", id_field="ids.9")
    )

    with pytest.raises(ValueError, match="missing its configured id"):
        adapter._normalize({"ids": ["only"], "name": "Challenge"})


@pytest.mark.parametrize(
    "item",
    [
        {"id": "", "name": "Challenge"},
        {"id": "1", "name": "   "},
    ],
)
def test_platform_adapter_rejects_empty_identity_fields(item):
    adapter = HttpJsonAdapter(FieldMapping(list_url="https://ctf.example.test/api"))

    with pytest.raises(ValueError, match="non-empty"):
        adapter._normalize(item)


def test_attachment_failure_closes_responses_and_removes_partial_batch(tmp_path):
    responses = iter(
        [
            FakeResponse(chunks=[b"ok"], headers={"Content-Length": "2"}),
            FakeResponse(chunks=[b"too-long"], headers={"Content-Length": "8"}),
        ]
    )
    seen: list[FakeResponse] = []

    def request_get(*args, **kwargs):
        response = next(responses)
        seen.append(response)
        return response

    adapter = HttpJsonAdapter(
        FieldMapping(list_url="https://ctf.example.test/api"),
        request_get=request_get,
        max_attachment_bytes=4,
    )
    challenge = PlatformChallenge(
        external_id="1",
        title="Files",
        category="misc",
        description="",
        attachment_urls=[
            "https://ctf.example.test/one.txt",
            "https://ctf.example.test/two.txt",
        ],
    )

    with pytest.raises(ValueError, match="exceeds 4 byte"):
        adapter.download_attachments(challenge, tmp_path)

    assert list(tmp_path.iterdir()) == []
    assert all(response.closed for response in seen)


@pytest.mark.parametrize(
    ("attachment_headers", "expected_headers"),
    [
        ({}, {}),
        ({"Authorization": "Bearer cdn-secret"}, {"Authorization": "Bearer cdn-secret"}),
    ],
)
def test_cross_origin_attachments_require_explicit_headers(
    tmp_path,
    attachment_headers,
    expected_headers,
):
    calls = []

    def request_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(chunks=[b"ok"])

    adapter = HttpJsonAdapter(
        FieldMapping(
            list_url="https://api.example.test/challenges",
            headers={"Authorization": "Bearer api-secret"},
            attachment_headers=attachment_headers,
        ),
        request_get=request_get,
    )
    challenge = PlatformChallenge(
        external_id="1",
        title="Cross-origin file",
        category="misc",
        description="",
        attachment_urls=["https://cdn.example.test/file.txt"],
    )

    adapter.download_attachments(challenge, tmp_path)

    assert calls[0][1]["headers"] == expected_headers


def test_attachment_headers_are_required_workflow_secrets():
    spec = PlatformWorkflowSpec.model_validate(
        {
            "name": "separate attachment auth",
            "challenges": {
                "list_url": "https://api.example.test/challenges",
                "attachment_base_url": "https://cdn.example.test/",
                "attachment_headers": [
                    {
                        "name": "Authorization",
                        "secret_name": "cdn_token",
                        "prefix": "Bearer ",
                    }
                ],
            },
        }
    )

    assert spec.required_secret_names() == {"cdn_token"}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test:bad/api",
        "https://example.test:0/api",
        "https://example.test\\@127.0.0.1/api",
        "https://example.test/api?access-token=literal",
    ],
)
def test_workflow_rejects_ambiguous_or_credential_urls(url):
    with pytest.raises(ValueError):
        PlatformWorkflowSpec.model_validate(
            {
                "name": "unsafe",
                "challenges": {"list_url": url},
            }
        )


@pytest.mark.parametrize("name", ["Host", "Content-Length", "Transfer-Encoding"])
def test_workflow_rejects_transport_control_headers(name):
    with pytest.raises(ValueError, match="controlled by the HTTP client"):
        SecretHeader(name=name, secret_name="platform_token")


def test_workflow_secret_validation_happens_before_storage_and_short_values_are_bounded():
    spec = PlatformWorkflowSpec.model_validate(
        {
            "name": "safe",
            "challenges": {
                "list_url": "https://example.test/api",
                "headers": [
                    {
                        "name": "Authorization",
                        "secret_name": "platform_token",
                        "prefix": "Bearer ",
                    }
                ],
            },
        }
    )
    normalized = _normalize_secrets({"platform_token": "a"})
    _validate_workflow_secret_names(spec, normalized)
    with pytest.raises(ValueError, match="unknown workflow secret"):
        _validate_workflow_secret_names(spec, {"unused": "long-enough"})
    with pytest.raises(ValueError, match="control characters"):
        _normalize_secrets({"platform_token": "bad\x00token"})

    assert _replace_secret_values("data a beta", normalized) == (
        "data {{secret.platform_token}} beta"
    )


def test_unknown_workflow_secret_does_not_create_a_partial_draft():
    class Store:
        called = False

        def create_workflow(self, *args, **kwargs):
            self.called = True
            raise AssertionError("invalid workflow must not reach storage")

    service = OpsAgentService.__new__(OpsAgentService)
    service.store = Store()
    with pytest.raises(ValueError, match="unknown workflow secret"):
        service.create_workflow(
            {
                "name": "safe",
                "challenges": {"list_url": "https://example.test/api"},
            },
            secrets_values={"unused": "not-used-by-the-workflow"},
        )

    assert service.store.called is False


class FakeRaw:
    def __init__(self, chunks: list[bytes], *, length: str | None = None):
        self._chunks = iter(chunks)
        self.headers = {} if length is None else {"Content-Length": length}
        self.closed = False

    def read(self, amount):
        return next(self._chunks, b"")

    def close(self):
        self.closed = True


def test_workflow_response_reader_is_bounded_and_closes_transport():
    raw = FakeRaw([b"1234", b"5"])
    with pytest.raises(NetworkPolicyError, match="exceeds 4 byte"):
        _read_bounded_content(raw, 4)
    assert raw.closed is True

    declared = FakeRaw([], length="100")
    with pytest.raises(NetworkPolicyError, match="exceeds 4 byte"):
        _read_bounded_content(declared, 4)
    assert declared.closed is True
