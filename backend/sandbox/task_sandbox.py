from __future__ import annotations

from contextlib import suppress
import io
import os
import shlex
import tarfile
import threading
from pathlib import Path, PurePosixPath

from backend.sandbox.docker_manager import _load_docker_sdk
from backend.sandbox.errors import (
    DockerConfigurationError,
    DockerImageError,
    SandboxStartupError,
    classify_docker_startup_error,
)
from backend.sandbox.sandbox import ExecResult
from backend.sandbox.webui_proxy import webui_proxy_manager

# Layout inside the per-task container.
WORKSPACE = "/workspace"
ATTACHMENTS_DIR = f"{WORKSPACE}/attachments"
SHARED_DIR = f"{WORKSPACE}/shared"


def _safe_segment(value: str) -> str:
    import re

    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return text or "workspace"


def task_container_name(project_id: str) -> str:
    return f"ipc-task-{_safe_segment(project_id)}"


def member_workdir(member: str) -> str:
    return f"{WORKSPACE}/{_safe_segment(member)}"


class TaskSandbox:
    """One Docker container per CTF task (project), shared by all its Members.

    Members are isolated by working directory (/workspace/<member>) rather than
    by container. Files under /workspace are visible to every Member so they can
    exchange artifacts (e.g. via /workspace/shared). The container is not
    memory-capped; concurrency is bounded by the task-slot limiter instead.
    """

    def __init__(
        self,
        project_id: str,
        image: str,
        env: dict[str, str] | None = None,
        network: bool = True,
        attachments_dir: str | Path | None = None,
    ):
        self.project_id = project_id
        self.image = image
        self.env = env or {}
        self.network = network
        self.attachments_dir = Path(attachments_dir) if attachments_dir is not None else None
        self._container = None
        self._client = None
        self._sdk = None
        self._preflight_complete = False
        self._container_name = task_container_name(project_id)
        self._shared_network: str | None = None
        self._lock = threading.RLock()
        self._member_dirs: set[str] = set()

    # ---- docker plumbing ----

    def _docker(self):
        if self._client is None:
            docker = _load_docker_sdk()
            try:
                client = docker.from_env()
            except Exception as exc:
                raise classify_docker_startup_error(
                    exc,
                    "configure Docker client",
                ) from exc
            self._sdk = docker
            self._client = client
        if not self._preflight_complete:
            ping = getattr(self._client, "ping", None)
            if callable(ping):
                try:
                    ping()
                except Exception as exc:
                    raise classify_docker_startup_error(
                        exc,
                        "contact Docker daemon",
                    ) from exc
            self._preflight_complete = True
        return self._client

    def preflight(self) -> None:
        """Validate Docker configuration/socket/daemon before solver reasoning."""

        self._docker()

    def _shared_network_name(self) -> str | None:
        configured = os.environ.get("IPC_MEMBER_DOCKER_NETWORK", "").strip()
        if configured:
            return configured
        hostname = os.environ.get("HOSTNAME", "").strip()
        if not hostname:
            return None
        try:
            current = self._docker().containers.get(hostname)
        except Exception:
            return None
        networks = current.attrs.get("NetworkSettings", {}).get("Networks", {})
        if not networks:
            return None
        for name in networks:
            if name != "bridge":
                return name
        return next(iter(networks), None)

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self._container is not None:
                return
            client = self._docker()
            from docker.errors import ImageNotFound, NotFound

            try:
                try:
                    container = client.containers.get(self._container_name)
                    container.reload()
                    if getattr(container, "status", "running") != "running":
                        container.start()
                except NotFound:
                    run_kwargs = {
                        "image": self.image,
                        "command": ["sleep", "infinity"],
                        "detach": True,
                        "name": self._container_name,
                        "working_dir": "/",
                        "extra_hosts": {"host.docker.internal": "host-gateway"},
                    }
                    if self.network:
                        self._shared_network = self._shared_network_name()
                        if self._shared_network:
                            run_kwargs["network"] = self._shared_network
                        else:
                            run_kwargs["network_mode"] = "bridge"
                    else:
                        run_kwargs["network_mode"] = "none"
                    try:
                        container = client.containers.run(**run_kwargs)
                    except ImageNotFound as exc:
                        raise DockerImageError(
                            f"task image '{self.image}' is unavailable; "
                            "build the ipc-task-image service first",
                            operation="create task container",
                        ) from exc
                    except Exception as exc:
                        raise classify_docker_startup_error(
                            exc,
                            "create task container",
                        ) from exc
                except Exception as exc:
                    if isinstance(exc, SandboxStartupError):
                        raise
                    raise classify_docker_startup_error(
                        exc,
                        "inspect or start task container",
                    ) from exc
                else:
                    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
                    self._shared_network = next(
                        (name for name in networks if name != "bridge"), None
                    )

                self._container = container
                self._init_workspace(container)
                self._copy_attachments()
            except Exception as exc:
                with suppress(Exception):
                    if "container" in locals():
                        container.remove(force=True)
                self._container = None
                if isinstance(exc, SandboxStartupError):
                    raise
                raise classify_docker_startup_error(
                    exc,
                    "initialize task container",
                ) from exc

    def _init_workspace(self, container) -> None:
        setup = (
            f"mkdir -p {shlex.quote(ATTACHMENTS_DIR)} {shlex.quote(SHARED_DIR)} "
            f"&& if [ -d /opt/ipc-tools/tools ] && [ ! -e {WORKSPACE}/tools ]; then "
            f"ln -s /opt/ipc-tools/tools {WORKSPACE}/tools; fi "
            f"&& if [ -f /tools.txt ]; then cp /tools.txt {WORKSPACE}/tools.txt; fi"
        )
        res = container.exec_run(["bash", "-lc", setup], workdir="/")
        if res.exit_code not in (0, None):
            detail = self._decode_output(res.output)
            raise DockerConfigurationError(
                f"failed to initialize task workspace: {detail.strip()}",
                operation="initialize task container",
            )

    @staticmethod
    def _decode_output(output) -> str:
        if isinstance(output, tuple):
            out, err = output
            return b"\n".join(part for part in (out, err) if part).decode(errors="replace")
        if isinstance(output, bytes):
            return output.decode(errors="replace")
        return str(output)

    def _copy_attachments(self) -> None:
        if self._container is None or self.attachments_dir is None:
            return
        src = self.attachments_dir
        if not src.exists() or not src.is_dir():
            return
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            for path in sorted(src.rglob("*"), key=lambda p: str(p.relative_to(src))):
                rel = path.relative_to(src)
                arcname = PurePosixPath(*rel.parts).as_posix()
                tar.add(path, arcname=arcname, recursive=False)
        stream.seek(0)
        self._container.put_archive(ATTACHMENTS_DIR, stream.getvalue())

    def stop(self) -> None:
        webui_proxy_manager.close_project(self.project_id)
        with self._lock:
            if self._container is not None:
                with suppress(Exception):
                    self._container.remove(force=True)
                self._container = None
            self._member_dirs.clear()

    # ---- member views ----

    def member_view(self, member: str) -> MemberSandbox:
        workdir = member_workdir(member)
        with self._lock:
            if workdir not in self._member_dirs:
                if self._container is None:
                    self.start()
                self._raw_exec(f"mkdir -p {shlex.quote(workdir)}", workdir="/", timeout=10)
                self._member_dirs.add(workdir)
        return MemberSandbox(self, member, workdir)

    # ---- exec / IO (member-scoped via workdir) ----

    def exec(self, command: str, workdir: str, timeout: int = 60) -> ExecResult:
        if self._container is None:
            self.start()
        return self._raw_exec(command, workdir=workdir, timeout=timeout)

    def _raw_exec(self, command: str, workdir: str, timeout: int = 60) -> ExecResult:
        wrapped = f"timeout -k 5s {timeout}s bash -lc {shlex.quote(command)}"
        env = self.env or None
        res = self._container.exec_run(
            ["bash", "-lc", wrapped], workdir=workdir, demux=True, environment=env
        )
        out, err = res.output if isinstance(res.output, tuple) else (res.output, None)
        stdout = out.decode(errors="replace") if out else ""
        stderr = err.decode(errors="replace") if err else ""
        timed_out = res.exit_code in (124, 137)
        return ExecResult(res.exit_code if res.exit_code is not None else -1, stdout, stderr, timed_out)

    def write_file(self, workdir: str, rel_path: str, content: str) -> None:
        if self._container is None:
            self.start()
        path = str(PurePosixPath(workdir) / rel_path)
        data = content.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name=PurePosixPath(rel_path).name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        stream.seek(0)
        parent = str(PurePosixPath(path).parent)
        self._raw_exec(f"mkdir -p {shlex.quote(parent)}", workdir="/", timeout=10)
        self._container.put_archive(parent, stream.getvalue())

    def read_file(self, workdir: str, rel_path: str) -> str | None:
        res = self.exec(f"cat {shlex.quote(rel_path)}", workdir=workdir, timeout=15)
        if not res.ok:
            return None
        return res.stdout

    # ---- webui proxy target ----

    def proxy_target_host(self) -> str:
        if self._container is None:
            raise RuntimeError("task container is not running")
        if self._shared_network:
            return self._container_name
        networks = self._container.attrs.get("NetworkSettings", {}).get("Networks", {})
        for data in networks.values():
            ip = data.get("IPAddress", "")
            if ip:
                return ip
        return self._container_name


class MemberSandbox:
    """A per-Member view onto a shared TaskSandbox.

    Implements the Sandbox protocol (backend/sandbox/sandbox.py) by delegating
    to the task container with a fixed working directory.
    """

    def __init__(self, task: TaskSandbox, member: str, workdir: str):
        self._task = task
        self.member = member
        self.name = f"{task.project_id}-{member}"
        self.workdir = workdir

    def start(self) -> None:
        self._task.start()

    def exec(self, command: str, timeout: int = 60) -> ExecResult:
        return self._task.exec(command, workdir=self.workdir, timeout=timeout)

    def write_file(self, rel_path: str, content: str) -> None:
        self._task.write_file(self.workdir, rel_path, content)

    def read_file(self, rel_path: str) -> str | None:
        return self._task.read_file(self.workdir, rel_path)

    def stop(self) -> None:
        # Member views do not own the container; stopping is a task-level action.
        return None

    def visible_attachment_path(self, filename: str, original_path: str | None = None) -> str:
        safe_name = PurePosixPath(filename).name
        return str(PurePosixPath(ATTACHMENTS_DIR) / safe_name)

    def expose_webui(self, project_id: str, member: str, port: int) -> str:
        self._task.start()
        target_host = self._task.proxy_target_host()
        handle = webui_proxy_manager.register(project_id, member, target_host, port)
        return handle.url
