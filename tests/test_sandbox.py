from __future__ import annotations

import sys
import types

import pytest

from backend.sandbox import docker_manager
from backend.core.resource_manager import ResourceManager
from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.network_manager import NetworkManager
from backend.sandbox.resource_limiter import ResourceLimiter
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


def test_resource_limiter_per_agent_cap():
    rl = ResourceLimiter(total_memory_gb=8, per_agent_memory_gb=5)
    assert rl.reserve("a", 5) is True
    assert rl.reserve("b", 5) is False  # 10 > 8 total
    assert rl.reserve("b", 3) is True   # 5+3 = 8 ok
    rl.release("a")
    assert rl.reserved_memory_gb == 3


def test_resource_limiter_rejects_over_per_agent():
    rl = ResourceLimiter(per_agent_memory_gb=5)
    assert rl.can_admit(6) is False
    assert rl.reserve("a", 6) is False


def test_resource_limiter_repeated_reserve_same_agent_replaces_reservation():
    rl = ResourceLimiter(total_memory_gb=8, per_agent_memory_gb=5)
    assert rl.reserve("a", 5) is True
    assert rl.reserve("a", 3) is True
    assert rl.reserved_memory_gb == 3
    assert rl.reserve("b", 5) is True
    assert rl.reserved_memory_gb == 8


def test_resource_manager_resets_leaked_reservations_when_no_sandboxes(tmp_path):
    rl = ResourceLimiter(total_memory_gb=5, per_agent_memory_gb=5)
    assert rl.reserve("ghost", 5) is True
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    manager = ResourceManager(rl, pool)

    assert manager.can_admit_member() is True
    assert rl.reserved_memory_gb == 0


def test_resource_manager_reclaims_orphaned_projects():
    class FakePool:
        def __init__(self):
            self.keys = [("proj_001", "aventurine"), ("proj_002", "jade")]
            self.stopped = []

        def active_keys(self):
            return list(self.keys)

        def stop_project(self, project_id):
            self.stopped.append(project_id)
            self.keys = [key for key in self.keys if key[0] != project_id]

    rl = ResourceLimiter(total_memory_gb=10, per_agent_memory_gb=5)
    assert rl.reserve("proj_001-aventurine", 5) is True
    assert rl.reserve("proj_002-jade", 5) is True
    pool = FakePool()
    manager = ResourceManager(rl, pool)

    reclaimed = manager.reclaim_orphaned_projects({"proj_002"})

    assert reclaimed == ["proj_001"]
    assert pool.stopped == ["proj_001"]
    assert rl.reserved_memory_gb == 10


def test_container_pool_isolated_workspaces(tmp_path):
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    sb1 = pool.get("proj_001", "aventurine")
    sb2 = pool.get("proj_001", "pearl")
    sb1.write_file("a.txt", "from aventurine")
    assert sb2.read_file("a.txt") is None  # separate workspaces
    # same member returns same sandbox
    assert pool.get("proj_001", "aventurine") is sb1


def test_container_pool_docker_isolates_member_containers_and_workdirs(monkeypatch):
    created = []

    class FakeDockerSandbox:
        def __init__(self, name, image, env, memory_gb, network, limiter, workdir):
            self.name = name
            self.image = image
            self.env = env
            self.memory_gb = memory_gb
            self.network = network
            self.limiter = limiter
            self.workdir = workdir
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

    monkeypatch.setattr(docker_manager, "DockerSandbox", FakeDockerSandbox)

    pool = ContainerPool(backend="docker", image="ipc-member:latest", limiter=ResourceLimiter())
    sb1 = pool.get("proj_001", "aventurine")
    sb2 = pool.get("proj_001", "pearl")

    assert sb1 is not sb2
    assert sb1.name == "proj_001-aventurine"
    assert sb2.name == "proj_001-pearl"
    assert sb1.workdir == "/workspace/proj_001/aventurine"
    assert sb2.workdir == "/workspace/proj_001/pearl"
    assert sb1.started is True
    assert sb2.started is True
    assert pool.get("proj_001", "aventurine") is sb1
    assert created == [sb1, sb2]


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


def test_docker_sandbox_releases_reservation_on_start_failure(monkeypatch):
    limiter = ResourceLimiter(total_memory_gb=5, per_agent_memory_gb=5)
    sb = docker_manager.DockerSandbox(name="broken", image="ipc-member:latest", limiter=limiter)

    monkeypatch.setattr(sb, "_docker", lambda: (_ for _ in ()).throw(RuntimeError("docker unavailable")))

    with pytest.raises(RuntimeError, match="docker unavailable"):
        sb.start()
    assert limiter.reserved_memory_gb == 0
