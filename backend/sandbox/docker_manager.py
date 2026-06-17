from __future__ import annotations


import importlib
import io
import shlex
import sys
import tarfile
from pathlib import Path, PurePosixPath

from backend.sandbox.resource_limiter import ResourceLimiter
from backend.sandbox.sandbox import ExecResult


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
    ):
        self.name = name
        self.image = image
        self.env = env or {}
        self.memory_gb = memory_gb
        self.network = network
        self.limiter = limiter
        self.workdir = workdir
        self._container = None
        self._client = None

    def _docker(self):
        if self._client is None:
            docker = _load_docker_sdk()
            self._client = docker.from_env()
        return self._client

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

            cname = f"ipc-member-{self.name}"
            try:
                existing = client.containers.get(cname)
                existing.remove(force=True)
            except NotFound:
                pass
            run_kwargs = {
                "image": self.image,
                "command": ["sleep", "infinity"],
                "detach": True,
                "name": cname,
                "working_dir": self.workdir,
                "mem_limit": f"{int(self.memory_gb)}g",
                "network_mode": "bridge" if self.network else "none",
                "extra_hosts": {"host.docker.internal": "host-gateway"},
            }
            self._container = client.containers.run(**run_kwargs)
            self.exec(f"mkdir -p {shlex.quote(self.workdir)}", timeout=10)
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
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except Exception:
                pass
            self._container = None
        if self.limiter is not None:
            self.limiter.release(self.name)
