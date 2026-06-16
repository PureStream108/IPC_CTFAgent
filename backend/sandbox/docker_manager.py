from __future__ import annotations


import io
import shlex
import tarfile
from pathlib import PurePosixPath

from backend.sandbox.resource_limiter import ResourceLimiter
from backend.sandbox.sandbox import ExecResult


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
            try:
                import docker  # lazy
            except ImportError as exc:
                raise RuntimeError(
                    "Docker sandbox requires the Python Docker SDK. Install with `pip install -e .[docker]`."
                ) from exc
            if not hasattr(docker, "from_env"):
                origin = getattr(docker, "__file__", None) or getattr(docker, "__path__", None)
                raise RuntimeError(
                    "Docker sandbox imported a non-SDK `docker` module "
                    f"({origin}). Install the Python Docker SDK and run from an environment "
                    "where it is not shadowed by a local docker/ directory."
                )
            self._client = docker.from_env()
        return self._client

    def start(self) -> None:
        if self._container is not None:
            return
        if self.limiter is not None and not self.limiter.reserve(self.name, self.memory_gb):
            raise RuntimeError(f"resource limit reached, cannot start sandbox {self.name}")
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
