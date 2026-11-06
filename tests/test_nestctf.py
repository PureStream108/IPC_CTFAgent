"""The nestctf adapter turns with_scene into the container/non-container split."""

from __future__ import annotations

import pytest

from backend.platform.factory import build_adapter
from backend.platform.mapping import FieldMapping
from backend.platform.nestctf.adapter import NestctfAdapter, NestctfClient, NestctfError
from backend.platform.nestctf.workflow import NestctfWorkflowInput


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def listing(*items):
    return {"code": "AD-000000", "data": {"total": len(items), "list": list(items)}}


def entry(resource_id, name, *, solved=False, direction="Misc", score=100):
    return {
        "resource_id": resource_id,
        "name": name,
        "score": score,
        "solved": 3,
        "direction": direction,
        "is_solved": solved,
    }


def detail(*, with_scene, desc="", attachment=None):
    return {
        "resource_id": "x",
        "name": "x",
        "desc": desc,
        "attachment": attachment or {},
        "with_scene": with_scene,
        "with_scene_inst": with_scene,
        "is_solved": False,
    }


class FakeClient(NestctfClient):
    def __init__(self, list_payload, details):
        super().__init__("https://nest.test:54443", "race1234", "jwt-token")
        self._list = list_payload
        self._details = details
        self.list_calls = 0
        self.detail_calls: list[str] = []
        self.downloads: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.downloads.append((url, kwargs.get("headers") or {}))
        if "/checkpoints/?" in url:
            self.list_calls += 1
            return FakeResponse(self._list)
        resource_id = url.rstrip("/").rsplit("/", 1)[-1]
        self.detail_calls.append(resource_id)
        return FakeResponse({"code": "AD-000000", "data": self._details[resource_id]})


def mapping(**overrides):
    base = {"platform": "nestctf", "list_url": "https://nest.test:54443"}
    base.update(overrides)
    return FieldMapping(**base)


def adapter_for(list_payload, details, **overrides):
    client = FakeClient(list_payload, details)
    return NestctfAdapter(client, mapping(**overrides)), client


def test_attachments_only_keeps_the_challenges_that_need_no_container():
    listing_payload = listing(
        entry("aa", "Handout Only"),
        entry("bb", "Scene Challenge"),
        entry("cc", "Also Handout", direction="Web"),
    )
    details = {
        "aa": detail(
            with_scene=False,
            desc="<p>read the zip</p>",
            attachment={"name": "a.zip", "url": "/cpms/upload/attachment/aa.zip"},
        ),
        "bb": detail(with_scene=True, desc="needs a scene"),
        "cc": detail(with_scene=False, desc="web"),
    }
    adapter, _ = adapter_for(listing_payload, details, attachments_only=True)

    challenges = adapter.fetch_challenges()

    assert [item.external_id for item in challenges] == ["aa", "cc"]
    assert [item.remote for item in challenges] == [False, False]
    assert challenges[0].description == "read the zip"
    assert challenges[0].attachment_urls == [
        "https://nest.test:54443/cpms/upload/attachment/aa.zip"
    ]
    assert challenges[1].category == "web"


def test_without_attachments_only_the_scene_challenges_stay_remote():
    listing_payload = listing(entry("aa", "Handout"), entry("bb", "Scene"))
    details = {"aa": detail(with_scene=False), "bb": detail(with_scene=True)}
    adapter, _ = adapter_for(listing_payload, details)

    challenges = adapter.fetch_challenges()

    assert [(item.external_id, item.remote) for item in challenges] == [
        ("aa", False),
        ("bb", True),
    ]
    assert adapter.supports_instances is False


def test_details_are_cached_between_polls_but_the_score_board_is_not():
    listing_payload = listing(entry("aa", "Handout"))
    details = {"aa": detail(with_scene=False, desc="d")}
    adapter, client = adapter_for(listing_payload, details, attachments_only=True)

    adapter.fetch_challenges()
    adapter.fetch_challenges()

    assert client.list_calls == 2
    assert client.detail_calls == ["aa"]


def test_excluded_challenges_never_reach_a_project():
    listing_payload = listing(entry("aa", "Keep"), entry("bb", "Huge Download"))
    details = {
        "aa": detail(with_scene=False),
        "bb": detail(with_scene=False),
    }
    adapter, client = adapter_for(
        listing_payload, details, attachments_only=True, exclude=["Huge Download"]
    )

    challenges = adapter.fetch_challenges()

    assert [item.title for item in challenges] == ["Keep"]
    # The excluded challenge must not even be fetched.
    assert client.detail_calls == ["aa"]


def test_api_error_code_raises_instead_of_returning_an_empty_list():
    class Broken:
        def get(self, url, **kwargs):
            return FakeResponse({"code": "AD-002005", "message": "no permission"})

    client = NestctfClient(
        "https://nest.test:54443", "race1234", "jwt", request_get=Broken().get
    )

    with pytest.raises(NestctfError, match="no permission"):
        client.checkpoints()


def test_preset_builds_an_attachment_only_race_with_the_native_submit():
    spec = NestctfWorkflowInput(
        base_url="https://nest.test:54443",
        race_id="570b5e4e531ec7f2ee253117fb617926",
        name="race",
    ).workflow_spec()

    assert spec.challenges.platform == "nestctf"
    assert spec.challenges.attachments_only is True
    assert spec.competition_id == "570b5e4e531ec7f2ee253117fb617926"
    assert spec.submit.url.endswith(
        "/570b5e4e531ec7f2ee253117fb617926/flag/"
    )
    assert spec.submit.headers[0].secret_name == "nestctf_token"
    assert spec.challenges.headers[0].prefix == "JWT "


def test_factory_installs_the_handout_cookie_on_every_request():
    built = build_adapter(
        mapping(attachments_only=True),
        credentials={"nestctf_token": "jwt", "nestctf_cookie": "mg=1"},
        competition_id="race1234",
    )

    assert isinstance(built, NestctfAdapter)
    assert built.mapping.headers["Cookie"] == "mg=1"
    assert built.client.headers["Authorization"] == "JWT jwt"


def test_factory_refuses_a_workflow_without_a_race_id():
    with pytest.raises(ValueError, match="race id"):
        build_adapter(mapping(), credentials={"nestctf_token": "jwt"}, competition_id="")
