from __future__ import annotations

import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
import requests

from backend.sandbox import docker_manager, task_sandbox
from backend.core.resource_manager import ResourceManager
from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.network_manager import NetworkManager
from backend.sandbox.resource_limiter import TaskSlotLimiter
from backend.sandbox.sandbox import LocalSandbox


def test_local_sandbox_exec_echo(tmp_path):
    sb = LocalSandbox("m1", tmp_path / "ws")
    sb.start()
    res = sb.exec("echo hello")
    assert res.ok
    assert "hello" in res.stdout


def test_local_sandbox_write_read(tmp_path):
    sb = LocalSandbox("m1", tmp_path / "ws")
    sb.start()
    sb.write_file("sub/note.txt", "secret data")
    assert sb.read_file("sub/note.txt") == "secret data"
    assert sb.read_file("missing.txt") is None


def test_local_sandbox_path_escape_blocked(tmp_path):
    sb = LocalSandbox("m1", tmp_path / "ws")
    sb.start()
    with pytest.raises(ValueError):
        sb.write_file("../escape.txt", "x")


def test_local_sandbox_timeout(tmp_path):
    sb = LocalSandbox("m1", tmp_path / "ws")
    sb.start()
    # python sleep is portable across win/linux
    res = sb.exec(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout=1)
    assert res.timed_out
    assert res.exit_code == 124


def test_local_sandbox_exposes_webui_via_proxy(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = f"origin {self.path}".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()

    sb = LocalSandbox("m1", tmp_path / "ws")
    sb.start()
    proxy_url = sb.expose_webui("proj_001", "aventurine", upstream.server_address[1])
    resp = requests.get(f"{proxy_url}/hello?x=1", timeout=5)

    assert resp.status_code == 200
    assert resp.text == "origin /hello?x=1"

    sb.stop()
    upstream.shutdown()
    upstream.server_close()
    thread.join(timeout=1)


def test_task_slot_limiter_is_project_scoped_and_idempotent():
    limiter = TaskSlotLimiter(max_concurrent_tasks=2)
    assert limiter.acquire("proj_001") is True
    assert limiter.acquire("proj_001") is True
    assert limiter.acquire("proj_002") is True
    assert limiter.acquire("proj_003") is False
    assert limiter.active_tasks() == ["proj_001", "proj_002"]
    limiter.release("proj_001")
    assert limiter.acquire("proj_003") is True


def test_resource_manager_reclaims_orphaned_projects():
    class FakePool:
        def __init__(self):
            self.keys = [("proj_001", "aventurine"), ("proj_002", "jade")]
            self.stopped = []

        def active_projects(self):
            return sorted({project_id for project_id, _ in self.keys})

        def stop_project(self, project_id):
            self.stopped.append(project_id)
            self.keys = [key for key in self.keys if key[0] != project_id]

    rl = TaskSlotLimiter(max_concurrent_tasks=2)
    assert rl.acquire("proj_001") is True
    assert rl.acquire("proj_002") is True
    pool = FakePool()
    manager = ResourceManager(rl, pool)

    reclaimed = manager.reclaim_orphaned_projects({"proj_002"})

    assert reclaimed == ["proj_001"]
    assert pool.stopped == ["proj_001"]
    assert rl.active_tasks() == ["proj_002"]


def test_container_pool_isolated_workspaces(tmp_path):
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    sb1 = pool.get("proj_001", "aventurine")
    sb2 = pool.get("proj_001", "pearl")
    sb1.write_file("a.txt", "from aventurine")
    assert sb2.read_file("a.txt") is None  # separate workspaces
    # same member returns same sandbox
    assert pool.get("proj_001", "aventurine") is sb1


def test_container_pool_docker_shares_one_task_container_between_members(tmp_path, monkeypatch):
    created = []

    class FakeMemberSandbox:
        def __init__(self, task, member):
            self.task = task
            self.name = f"{task.project_id}-{member}"
            self.workdir = f"/workspace/{member}"
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    class FakeTaskSandbox:
        def __init__(self, project_id, image, env, network, attachments_dir=None):
            self.project_id = project_id
            self.image = image
            self.env = env
            self.network = network
            self.attachments_dir = attachments_dir
            self.views = {}
            created.append(self)

        def member_view(self, member):
            return self.views.setdefault(member, FakeMemberSandbox(self, member))

        def stop(self):
            return None

    monkeypatch.setattr(task_sandbox, "TaskSandbox", FakeTaskSandbox)

    pool = ContainerPool(
        backend="docker",
        image="ipc-task:latest",
        limiter=TaskSlotLimiter(),
        workspace_root=tmp_path / "projects",
    )
    sb1 = pool.get("proj_001", "aventurine")
    sb2 = pool.get("proj_001", "pearl")

    assert sb1 is not sb2
    assert sb1.name == "proj_001-aventurine"
    assert sb2.name == "proj_001-pearl"
    assert sb1.workdir == "/workspace/aventurine"
    assert sb2.workdir == "/workspace/pearl"
    assert sb1.task is sb2.task
    assert sb1.task.attachments_dir == tmp_path / "projects" / "proj_001" / "attachments"
    assert sb1.started is True
    assert sb2.started is True
    assert pool.get("proj_001", "aventurine") is sb1
    assert created == [sb1.task]
    assert pool.active_projects() == ["proj_001"]


def test_container_pool_stop_project(tmp_path):
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    pool.get("proj_001", "aventurine")
    pool.get("proj_001", "pearl")
    pool.get("proj_002", "jade")
    pool.stop_project("proj_001")
    keys = pool.active_keys()
    assert ("proj_001", "aventurine") not in keys
    assert ("proj_002", "jade") in keys


def test_container_pool_removes_failed_sandbox_from_cache(tmp_path, monkeypatch):
    class BrokenSandbox:
        name = "broken"

        def start(self):
            raise RuntimeError("boom")

        def stop(self):
            return None

    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    monkeypatch.setattr(pool, "_create", lambda project_id, member, env: BrokenSandbox())

    with pytest.raises(RuntimeError, match="boom"):
        pool.get("proj_001", "aventurine")
    assert pool.active_keys() == []


def test_network_manager_detects_compose(tmp_path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    nm = NetworkManager(backend="local")
    env = nm.start("proj_001", att)
    assert env is not None
    assert env.started is True
    assert env.network_name == "ipc-proj-proj_001"
    nm.stop("proj_001")
    assert nm.get("proj_001") is None


def test_network_manager_no_docker_files(tmp_path):
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "challenge.bin").write_text("x", encoding="utf-8")
    nm = NetworkManager(backend="local")
    assert nm.start("proj_001", att) is None


def test_load_docker_sdk_skips_repo_local_shadow(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    local_docker_dir = repo_root / "docker"
    local_docker_dir.mkdir(parents=True)

    shadow = types.SimpleNamespace(__path__=[str(local_docker_dir)])
    sdk = types.SimpleNamespace(from_env=lambda: "client")
    import_calls: list[list[str]] = []

    def fake_import_module(name: str):
        assert name == "docker"
        import_calls.append(list(docker_manager.sys.path))
        module = shadow if len(import_calls) == 1 else sdk
        docker_manager.sys.modules[name] = module
        return module

    monkeypatch.setattr(docker_manager, "_project_root", lambda: repo_root)
    monkeypatch.setattr(docker_manager.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(docker_manager.sys, "path", ["", str(repo_root), "/site-packages"])
    monkeypatch.setitem(docker_manager.sys.modules, "docker", shadow)
    monkeypatch.chdir(repo_root)

    loaded = docker_manager._load_docker_sdk()

    assert loaded is sdk
    assert import_calls[0] == ["", str(repo_root), "/site-packages"]
    assert import_calls[1] == ["/site-packages"]
    assert docker_manager.sys.modules["docker"] is sdk


def test_task_sandbox_clears_container_on_start_failure(monkeypatch):
    sb = task_sandbox.TaskSandbox(project_id="broken", image="ipc-task:latest")

    monkeypatch.setattr(sb, "_docker", lambda: (_ for _ in ()).throw(RuntimeError("docker unavailable")))

    with pytest.raises(RuntimeError, match="docker unavailable"):
        sb.start()
    assert sb._container is None


def test_task_sandbox_initializes_shared_workspace_and_copies_attachments(tmp_path, monkeypatch):
    from docker.errors import NotFound

    attachments = tmp_path / "attachments"
    attachments.mkdir()
    (attachments / "challenge.bin").write_bytes(b"binary")

    class FakeContainer:
        attrs = {"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2"}}}}

        def __init__(self):
            self.commands = []
            self.archives = []

        def exec_run(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return types.SimpleNamespace(exit_code=0, output=b"")

        def put_archive(self, path, data):
            self.archives.append((path, data))

        def remove(self, force=False):
            return None

    container = FakeContainer()

    class FakeContainers:
        def __init__(self):
            self.run_kwargs = None

        def get(self, name):
            raise NotFound("missing")

        def run(self, **kwargs):
            self.run_kwargs = kwargs
            return container

    containers = FakeContainers()
    client = types.SimpleNamespace(containers=containers)
    sandbox = task_sandbox.TaskSandbox(
        project_id="proj_001",
        image="ipc-task:latest",
        attachments_dir=attachments,
    )
    monkeypatch.setattr(sandbox, "_docker", lambda: client)
    monkeypatch.setattr(sandbox, "_shared_network_name", lambda: None)

    sandbox.start()
    aventurine = sandbox.member_view("aventurine")
    pearl = sandbox.member_view("pearl")

    assert containers.run_kwargs["name"] == "ipc-task-proj_001"
    assert "mem_limit" not in containers.run_kwargs
    assert container.archives and container.archives[0][0] == "/workspace/attachments"
    assert aventurine._task is pearl._task is sandbox
    assert aventurine.workdir == "/workspace/aventurine"
    assert pearl.workdir == "/workspace/pearl"
