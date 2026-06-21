from __future__ import annotations


import importlib
import io
import os
import shlex
import sys
import tarfile
from pathlib import Path, PurePosixPath

from backend.sandbox.resource_limiter import ResourceLimiter
from backend.sandbox.sandbox import ExecResult
from backend.sandbox.webui_proxy import webui_proxy_manager


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _docker_module_origin(module) -> str:
    origin = getattr(module, "__file__", None)
    if origin:
        return str(origin)
    paths = getattr(module, "__path__", None)
    if paths:
        return str(list(paths))
    return "<unknown>"


def _is_repo_local_docker_module(module, project_root: Path | None = None) -> bool:
    root = (project_root or _project_root()).resolve()
    local_docker_dir = (root / "docker").resolve()
    candidates: list[Path] = []

    origin = getattr(module, "__file__", None)
    if origin:
        candidates.append(Path(origin))

    module_paths = getattr(module, "__path__", None)
    if module_paths:
        for entry in module_paths:
            candidates.append(Path(entry))

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == local_docker_dir or local_docker_dir in resolved.parents:
            return True
    return False


def _filtered_sys_path(project_root: Path) -> list[str]:
    root = project_root.resolve()
    filtered: list[str] = []
    for entry in sys.path:
        if entry == "":
            try:
                if Path.cwd().resolve() == root:
                    continue
            except OSError:
                pass
            filtered.append(entry)
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            filtered.append(entry)
            continue
        if resolved == root:
            continue
        filtered.append(entry)
    return filtered


def _load_docker_sdk():
    try:
        docker = importlib.import_module("docker")
    except ImportError as exc:
        initial_import_error = exc
    else:
        if hasattr(docker, "from_env") and not _is_repo_local_docker_module(docker):
            return docker
        initial_import_error = None

    project_root = _project_root()
    saved_path = list(sys.path)
    previous_module = sys.modules.pop("docker", None)
    loaded_sdk = False
    try:
        sys.path[:] = _filtered_sys_path(project_root)
        docker = importlib.import_module("docker")
        if hasattr(docker, "from_env") and not _is_repo_local_docker_module(docker, project_root):
            loaded_sdk = True
            return docker
        origin = _docker_module_origin(docker)
        raise RuntimeError(
            "Docker sandbox imported a non-SDK `docker` module "
            f"({origin}). Install the Python Docker SDK and run from an environment "
            "where it is not shadowed by a local docker/ directory."
        )
    except ImportError as exc:
        raise RuntimeError(
            "Docker sandbox requires the Python Docker SDK. Install with `pip install -e .[docker]`."
        ) from (initial_import_error or exc)
    finally:
        sys.path[:] = saved_path
        if not loaded_sdk:
            if previous_module is not None:
                sys.modules["docker"] = previous_module
            else:
                sys.modules.pop("docker", None)


class DockerSandbox:
    def __init__(
        self,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        memory_gb: float = 5,
        network: bool = True,
        limiter: ResourceLimiter | None = None,
        workdir: str = "/workspace",
        attachments_dir: str | Path | None = None,
    ):
        self.name = name
        self.image = image
        self.env = env or {}
        self.memory_gb = memory_gb
        self.network = network
        self.limiter = limiter
        self.workdir = workdir
        self.attachments_dir = Path(attachments_dir) if attachments_dir is not None else None
        self.attachment_mount_path = str(PurePosixPath(self.workdir).parent / "attachments")
        self._container = None
        self._client = None
        self._container_name = f"ipc-member-{self.name}"
        self._shared_network: str | None = None
        self._webui_keys: set[tuple[str, str]] = set()

    def _docker(self):
        if self._client is None:
            docker = _load_docker_sdk()
            self._client = docker.from_env()
        return self._client

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

    def start(self) -> None:
        if self._container is not None:
            return
        reserved = False
        try:
            if self.limiter is not None and not self.limiter.reserve(self.name, self.memory_gb):
                raise RuntimeError(f"resource limit reached, cannot start sandbox {self.name}")
            reserved = self.limiter is not None
            client = self._docker()
            from docker.errors import NotFound

            try:
                existing = client.containers.get(self._container_name)
                existing.remove(force=True)
            except NotFound:
                pass
            run_kwargs = {
                "image": self.image,
                "command": ["sleep", "infinity"],
                "detach": True,
                "name": self._container_name,
                "working_dir": "/",
                "mem_limit": f"{int(self.memory_gb)}g",
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
            self._container = client.containers.run(**run_kwargs)
            quoted_workdir = shlex.quote(self.workdir)
            setup = (
                f"mkdir -p {quoted_workdir} "
                f"&& if [ -d /opt/ipc-tools/tools ] && [ ! -e {quoted_workdir}/tools ]; then "
                f"ln -s /opt/ipc-tools/tools {quoted_workdir}/tools; fi"
            )
            setup_res = self._container.exec_run(["bash", "-lc", setup], workdir="/")
            if setup_res.exit_code not in (0, None):
                output = setup_res.output
                if isinstance(output, tuple):
                    out, err = output
                    detail = b"\n".join(part for part in (out, err) if part).decode(errors="replace")
                elif isinstance(output, bytes):
                    detail = output.decode(errors="replace")
                else:
                    detail = str(output)
                raise RuntimeError(f"failed to initialize sandbox workspace {self.workdir}: {detail.strip()}")
            self._copy_attachments()
        except Exception:
            if self._container is not None:
                try:
                    self._container.remove(force=True)
                except Exception:
                    pass
                self._container = None
            if reserved and self.limiter is not None:
                self.limiter.release(self.name)
            raise

    def visible_attachment_path(self, filename: str, original_path: str | None = None) -> str:
        safe_name = PurePosixPath(filename).name
        return str(PurePosixPath(self.attachment_mount_path) / safe_name)

    def _copy_attachments(self) -> None:
        if self._container is None or self.attachments_dir is None:
            return
        src = self.attachments_dir
        if not src.exists() or not src.is_dir():
            return
        project_root = str(PurePosixPath(self.workdir).parent)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            root_info = tarfile.TarInfo("attachments")
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            tar.addfile(root_info)
            for path in sorted(src.rglob("*"), key=lambda p: str(p.relative_to(src))):
                rel = path.relative_to(src)
                arcname = PurePosixPath("attachments", *rel.parts).as_posix()
                tar.add(path, arcname=arcname, recursive=False)
        stream.seek(0)
        self._container.put_archive(project_root, stream.getvalue())

    def exec(self, command: str, timeout: int = 60) -> ExecResult:
        if self._container is None:
            self.start()
        wrapped = f"timeout -k 5s {timeout}s bash -lc {shlex.quote(command)}"
        env = self.env or None
        res = self._container.exec_run(
            ["bash", "-lc", wrapped], workdir=self.workdir, demux=True, environment=env
        )
        out, err = res.output if isinstance(res.output, tuple) else (res.output, None)
        stdout = out.decode(errors="replace") if out else ""
        stderr = err.decode(errors="replace") if err else ""
        timed_out = res.exit_code in (124, 137)
        return ExecResult(res.exit_code if res.exit_code is not None else -1, stdout, stderr, timed_out)

    def write_file(self, rel_path: str, content: str) -> None:
        if self._container is None:
            self.start()
        path = str(PurePosixPath(self.workdir) / rel_path)
        data = content.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name=PurePosixPath(rel_path).name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        stream.seek(0)
        parent = str(PurePosixPath(path).parent)
        self.exec(f"mkdir -p {shlex.quote(parent)}", timeout=10)
        self._container.put_archive(parent, stream.getvalue())

    def read_file(self, rel_path: str) -> str | None:
        res = self.exec(f"cat {shlex.quote(rel_path)}", timeout=15)
        if not res.ok:
            return None
        return res.stdout

    def stop(self) -> None:
        for project_id, member in list(self._webui_keys):
            webui_proxy_manager.close_member(project_id, member)
        self._webui_keys.clear()
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except Exception:
                pass
            self._container = None
        if self.limiter is not None:
            self.limiter.release(self.name)

    def expose_webui(self, project_id: str, member: str, port: int) -> str:
        if self._container is None:
            self.start()
        target_host = self._proxy_target_host()
        handle = webui_proxy_manager.register(project_id, member, target_host, port)
        self._webui_keys.add((project_id, member))
        return handle.url

    def _proxy_target_host(self) -> str:
        if self._container is None:
            raise RuntimeError("sandbox container is not running")
        if self._shared_network:
            return self._container_name
        networks = self._container.attrs.get("NetworkSettings", {}).get("Networks", {})
        for data in networks.values():
            ip = data.get("IPAddress", "")
            if ip:
                return ip
        return self._container_name
