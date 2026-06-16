from __future__ import annotations

import sys

import pytest

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


def test_container_pool_isolated_workspaces(tmp_path):
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    sb1 = pool.get("proj_001", "aventurine")
    sb2 = pool.get("proj_001", "pearl")
    sb1.write_file("a.txt", "from aventurine")
    assert sb2.read_file("a.txt") is None  # separate workspaces
    # same member returns same sandbox
    assert pool.get("proj_001", "aventurine") is sb1


def test_container_pool_stop_project(tmp_path):
    pool = ContainerPool(backend="local", workspace_root=tmp_path)
    pool.get("proj_001", "aventurine")
    pool.get("proj_001", "pearl")
    pool.get("proj_002", "jade")
    pool.stop_project("proj_001")
    keys = pool.active_keys()
    assert ("proj_001", "aventurine") not in keys
    assert ("proj_002", "jade") in keys


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
