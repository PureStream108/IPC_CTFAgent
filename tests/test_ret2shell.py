from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.blackboard import graph_store
from backend.mcp.mcp_client import MCPClient
from backend.platform.mapping import FieldMapping
from backend.platform.ret2shell import (
    Ret2ShellAdapter,
    Ret2ShellAuthError,
    Ret2ShellClient,
    Ret2ShellError,
    Ret2ShellPreflightError,
    Ret2ShellRateLimitError,
    _SubmitRateLimiter,
)
from backend.platform.ret2shell_mcp import build_ret2shell_mcp
from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config

BASE = "https://r2s.test"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, content=None):
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""
        self.headers = headers or {}
        if content is not None:
            self._content = content
        elif payload is not None:
            self._content = json.dumps(payload).encode()
        else:
            self._content = b""

    @property
    def content(self):
        return self._content

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload

    def iter_content(self, chunk_size):
        yield self._content


class FakeSession:
    def __init__(self, router):
        self.router = router
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.router(method, url, kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def close(self):
        pass


def make_client(router, authed=True, **kwargs):
    defaults = dict(
        base_url=BASE,
        game_id=37,
        username="user",
        password="pass",
        poll_attempts=3,
        poll_interval=0.0,
        sleep=lambda seconds: None,
    )
    if authed:
        defaults["token"] = "seed-token"
    defaults.update(kwargs)
    return Ret2ShellClient(session=FakeSession(router), **defaults)


def login_ok(url, token="token-1"):
    if url.endswith("/api/account/login"):
        return FakeResponse(200, headers={"Set-Token": token})
    return None


# ---- auth ----


def test_login_captures_set_token_header_and_uses_bearer():
    def router(method, url, kwargs):
        if login := login_ok(url):
            return login
        if url.endswith("/api/account/profile"):
            return FakeResponse(200, {"account": "user"})
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router, authed=False)
    profile = client.get_profile()
    assert profile["account"] == "user"
    auth = client.session.calls[1][2]["headers"]["Authorization"]
    assert auth == "Bearer token-1"
    client.close()


def test_login_failure_raises_auth_error():
    def router(method, url, kwargs):
        if url.endswith("/api/account/login"):
            return FakeResponse(401, text="account or password is wrong")
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router, authed=False)
    with pytest.raises(Ret2ShellAuthError, match="account or password is wrong"):
        client.get_profile()
    client.close()


def test_set_token_refresh_rolls_token_forward():
    state = {"logins": 0}

    def router(method, url, kwargs):
        if url.endswith("/api/account/login"):
            state["logins"] += 1
            return FakeResponse(200, headers={"Set-Token": "token-1"})
        if url.endswith("/api/game/37"):
            if state["logins"] == 1:
                return FakeResponse(200, {"id": 37}, headers={"Set-Token": "token-2"})
            return FakeResponse(200, {"id": 37})
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router, authed=False)
    client.get_game()
    client.get_game()
    auth = client.session.calls[-1][2]["headers"]["Authorization"]
    assert auth == "Bearer token-2"
    assert state["logins"] == 1
    client.close()


def test_expired_token_relogins_once():
    state = {"profile_calls": 0}

    def router(method, url, kwargs):
        if url.endswith("/api/account/login"):
            return FakeResponse(200, headers={"Set-Token": "token-2"})
        if url.endswith("/api/account/profile"):
            state["profile_calls"] += 1
            if state["profile_calls"] == 1:
                return FakeResponse(401, text="token expired")
            return FakeResponse(200, {"account": "user"})
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router, authed=False)
    assert client.get_profile()["account"] == "user"
    assert client.session.calls[0][0] == "POST"  # login first
    assert client.session.calls[-1][2]["headers"]["Authorization"] == "Bearer token-2"
    client.close()


# ---- challenges / attachments ----


def test_list_challenges_parses_items_total_tuple():
    def router(method, url, kwargs):
        if url.endswith("/api/game/37/challenge"):
            return FakeResponse(
                200,
                [
                    [{"id": 1, "name": "Warmup", "tag": [{"name": "pwn", "primary": True}]}],
                    1,
                ],
            )
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    challenges = client.list_challenges()
    assert challenges[0]["name"] == "Warmup"
    client.close()


def test_error_detail_uses_plain_text_body():
    def router(method, url, kwargs):
        if url.endswith("/api/game/37/challenge"):
            return FakeResponse(412, text="game has not started")
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    with pytest.raises(Ret2ShellError, match="game has not started"):
        client.list_challenges()
    client.close()


def test_adapter_maps_categories_and_downloads_files(tmp_path):
    files = [{"folder": "static", "file": "handout.zip"}]

    class StubClient:
        game_id = 37

        def list_challenges(self, game_id=None):
            return [
                {"id": 1, "name": "Pwn Me", "tag": [{"name": "pwn", "primary": True}], "content": "nc"},
                {"id": 2, "name": "Odd", "tag": [{"name": "weird", "primary": True}], "content": ""},
                {"id": 3, "name": "NoTag", "content": ""},
            ]

        def list_files(self, challenge_id, game_id=None):
            return files

        def download_file(self, challenge_id, folder, file, dest_dir, game_id=None, *, max_bytes=None):
            target = Path(dest_dir) / file
            target.write_bytes(b"zip-bytes")
            return target

    adapter = Ret2ShellAdapter(StubClient(), category_map={"weird": "reverse"})
    challenges = adapter.fetch_challenges()
    assert [c.category for c in challenges] == ["pwn", "reverse", "misc"]
    assert [c.external_id for c in challenges] == ["1", "2", "3"]
    assert challenges[0].attachment_urls == []

    downloaded = adapter.download_attachments(challenges[0], tmp_path)
    assert downloaded[0].name == "handout.zip"
    assert downloaded[0].read_bytes() == b"zip-bytes"


def test_download_file_streams_to_disk(tmp_path):
    def router(method, url, kwargs):
        if url.endswith("/api/game/37/challenge/5/file") and kwargs.get("params", {}).get("file"):
            return FakeResponse(200, content=b"attachment-data")
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    target = client.download_file(5, "static", "handout.tar.gz", tmp_path)
    assert target.name == "handout.tar.gz"
    assert target.read_bytes() == b"attachment-data"
    call = client.session.calls[0]
    assert call[2]["params"] == {"folder": "static", "file": "handout.tar.gz"}
    client.close()


# ---- instances ----


def test_instance_lifecycle_and_wait_for_endpoints():
    state = {"polls": 0, "started": False}

    def router(method, url, kwargs):
        if url.endswith("/api/game/37/challenge/5/instance") and method == "POST":
            state["started"] = True
            return FakeResponse(200)
        if url.endswith("/api/game/37/instance"):
            state["polls"] += 1
            exposed = [{"1.2.3.4:1337": {"protocol": "tcp", "port": 1337}}] if state["polls"] >= 2 else None
            return FakeResponse(
                200,
                [
                    {
                        "state": "Running",
                        "challenge_id": 5,
                        "exposed_ports": exposed,
                        "renew_count": 0,
                    }
                ],
            )
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    client.start_instance(5)
    assert state["started"] is True
    instance = client.wait_for_instance(5, timeout=10, interval=0)
    assert instance["exposed_ports"]
    client.close()


def test_wait_for_instance_times_out():
    def router(method, url, kwargs):
        if url.endswith("/api/game/37/instance"):
            return FakeResponse(200, [{"state": "Pending", "challenge_id": 5, "exposed_ports": None}])
        raise AssertionError(f"unexpected {method} {url}")

    clock = {"now": 0.0}
    client = make_client(
        router,
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    with pytest.raises(Ret2ShellError, match="did not become reachable"):
        client.wait_for_instance(5, timeout=3, interval=1)
    client.close()


# ---- submission ----


def _submit_router(submissions):
    state = {"polls": 0}

    def router(method, url, kwargs):
        if login := login_ok(url):
            return login
        if url.endswith("/api/game/37/challenge/5/submit"):
            if method == "POST":
                submissions.append({"id": 900, "solved": None, "result": None})
                return FakeResponse(200, submissions[-1])
            state["polls"] += 1
            submission = submissions[0]
            if state["polls"] >= 2 and submission["solved"] is None:
                submission["solved"] = True
                submission["result"] = "accepted"
            return FakeResponse(200, submission)
        raise AssertionError(f"unexpected {method} {url}")

    return router


def test_submit_flag_polls_async_verdict():
    submissions = []
    client = make_client(_submit_router(submissions))
    result = client.submit_flag(5, "flag{demo}", check_solved=False)
    assert result["solved"] is True
    assert result["result"] == "accepted"
    client.close()


def test_submit_flag_preflight_refuses_solved_challenge():
    def router(method, url, kwargs):
        if login := login_ok(url):
            return login
        if url.endswith("/api/game/37/challenge/5/submit") and method == "GET":
            return FakeResponse(200, {"solved": True, "solves": 12})
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    with pytest.raises(Ret2ShellPreflightError, match="already solved"):
        client.submit_flag(5, "flag{demo}")
    posts = [call for call in client.session.calls if call[0] == "POST" and call[1].endswith("/submit")]
    assert posts == []
    client.close()


def test_submit_flag_rejects_empty_flag():
    client = make_client(lambda *args: FakeResponse(200, {}))
    with pytest.raises(Ret2ShellPreflightError, match="flag must not be empty"):
        client.submit_flag(5, "   ")
    client.close()


def test_submit_flag_maps_http_429_to_rate_limit():
    def router(method, url, kwargs):
        if login := login_ok(url):
            return login
        if url.endswith("/api/game/37/challenge/5/submit"):
            if method == "POST":
                return FakeResponse(429, text="too many submissions, please calmdown")
            return FakeResponse(200, {"solved": False})
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router)
    with pytest.raises(Ret2ShellRateLimitError, match="too many submissions"):
        client.submit_flag(5, "flag{demo}", check_solved=False)
    client.close()


def test_submit_flag_poll_timeout_keeps_solved_none():
    submissions = [{"id": 900, "solved": None, "result": None}]

    def router(method, url, kwargs):
        if login := login_ok(url):
            return login
        if url.endswith("/api/game/37/challenge/5/submit"):
            if method == "POST":
                return FakeResponse(200, submissions[0])
            return FakeResponse(200, submissions[0])
        raise AssertionError(f"unexpected {method} {url}")

    client = make_client(router, poll_attempts=2)
    result = client.submit_flag(5, "flag{demo}", check_solved=False)
    assert result["solved"] is None
    gets = [c for c in client.session.calls if c[0] == "GET" and c[1].endswith("/submit")]
    assert len(gets) == 2
    client.close()


def test_rate_limiter_enforces_shared_quota():
    clock = {"now": 0.0}
    limiter = _SubmitRateLimiter(monotonic=lambda: clock["now"])
    for _ in range(10):
        limiter.reserve()
    with pytest.raises(Ret2ShellRateLimitError, match="retry in 300s"):
        limiter.reserve()
    clock["now"] = 301.0
    limiter.reserve()  # window expired
    assert limiter.used() == 1


# ---- MCP ----


class StubPlatformClient:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def find_instance(self, challenge_id, game_id=None):
        self.calls.append(("find", challenge_id))
        return None

    def start_instance(self, challenge_id, game_id=None):
        self.calls.append(("start", challenge_id))

    def wait_for_instance(self, challenge_id, game_id=None, **kwargs):
        self.calls.append(("wait", challenge_id))
        return {
            "state": "Running",
            "exposed_ports": [{"ctf.r2s.test:1337": {"protocol": "tcp"}}],
            "renew_count": 0,
        }

    def renew_instance(self, challenge_id, game_id=None):
        self.calls.append(("renew", challenge_id))

    def destroy_instance(self, challenge_id, game_id=None):
        self.calls.append(("destroy", challenge_id))

    def challenge_status(self, challenge_id, game_id=None):
        self.calls.append(("status", challenge_id))
        return {"solved": False, "solves": 3}

    def close(self):
        pass


def _call_tool(server, name, **arguments):
    async def run():
        async with MCPClient.in_process(server) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert name in tools
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_ret2shell_mcp_exposes_instance_tools():
    stub = StubPlatformClient()
    server = build_ret2shell_mcp(stub)
    started = _call_tool(server, "instance_start", challenge_id=5)
    assert started["endpoints"]
    assert ("start", 5) in stub.calls and ("wait", 5) in stub.calls
    assert _call_tool(server, "instance_status", challenge_id=5)["running"] is False
    assert _call_tool(server, "instance_renew", challenge_id=5)["renewed"] is True
    assert _call_tool(server, "instance_stop", challenge_id=5)["stopped"] is True
    status = _call_tool(server, "challenge_status", challenge_id=5)
    assert status["solved"] is False and status["solves"] == 3


# ---- API import integration ----


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    config_dir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as test_client:
        setup_test_auth(test_client)
        test_client.app.state.ipc.config_dir = config_dir
        test_client.app.state.ipc.reload_config()
        yield test_client


def test_platform_import_supports_ret2shell_platform(api_client, monkeypatch):
    class StubClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_challenges(self, game_id=None):
            return [
                {
                    "id": 11,
                    "name": "Remote Calc",
                    "tag": [{"name": "pwn", "primary": True}],
                    "content": "nc target 1337",
                }
            ]

        def list_files(self, challenge_id, game_id=None):
            return [{"folder": "static", "file": "calc.zip"}]

        def download_file(self, challenge_id, folder, file, dest_dir, game_id=None, *, max_bytes=None):
            target = Path(dest_dir) / file
            target.write_bytes(b"zip")
            return target

        def close(self):
            pass

    monkeypatch.setattr("backend.api.platform.Ret2ShellClient", StubClient)
    mapping = FieldMapping(platform="ret2shell", game_id=37)

    preview = api_client.post(
        "/api/platform/challenges", json={"mapping": mapping.model_dump()}
    )
    assert preview.status_code == 200
    challenges = preview.json()["challenges"]
    assert challenges[0]["external_id"] == "11"
    assert challenges[0]["category"] == "pwn"

    imported = api_client.post(
        "/api/platform/import",
        json={"mapping": mapping.model_dump(), "select": ["11"]},
    )
    assert imported.status_code == 201
    item = imported.json()["imported"][0]
    assert item["category"] == "pwn"
    with api_client.app.state.ipc.db.connect() as conn:
        graph_store.set_flag(conn, item["project_id"], "flag{r2s}")
        graph_store.set_status(conn, item["project_id"], "completed")
    assert api_client.get(f"/api/flags/{item['project_id']}").json()["flag"] == "flag{r2s}"


def test_field_mapping_requires_list_url_for_http_json():
    with pytest.raises(ValueError, match="list_url is required"):
        FieldMapping()
    assert FieldMapping(platform="ret2shell").game_id is None
