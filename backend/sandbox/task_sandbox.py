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
# Detached job state. Deliberately inside the workspace volume: a docker exec
# id dies with a container restart, while these files survive it, so a job's
# fate stays knowable.
JOBS_DIR = f"{WORKSPACE}/.ipc/jobs"


def _safe_segment(value: str) -> str:
    import re

    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return text or "workspace"


def job_dir(job_id: str) -> str:
    return f"{JOBS_DIR}/{_safe_segment(job_id)}"


# The detached-job shell text lives in module functions rather than inline in
# the methods so it can be tested against a real bash without Docker. Quoting
# is the whole risk here: the command is model-supplied and is nested two
# levels deep (bash -c 'bash -lc "<command>"').


def job_launch_script(directory: str, command: str, workdir: str) -> str:
    """Shell that starts ``command`` detached and records its exit status.

    Two details carry the durability:

    * ``set -m`` enables job control, which puts the background job in its own
      process group.  That is what lets ``job_cancel_script`` kill the whole
      tree with one negative PID instead of orphaning grandchildren that still
      hold the output files open.  ``setsid`` would do the same but is not
      present in every shell the tests and dev sandboxes use, and a silent
      ``command not found`` there means the job never starts at all.
    * The supervisor writes ``status`` only after the real command exits, so a
      missing ``status`` file means "did not finish", never "succeeded".

    Written as separate statements rather than one ``&&`` chain on purpose:
    ``&`` binds more loosely than ``&&``, so a single chain ending in ``&``
    backgrounds the *whole* chain - including the ``mkdir`` - and the following
    write of the pid file then races a directory that does not exist yet.
    """
    quoted = shlex.quote(directory)
    supervisor = (
        f"bash -lc {shlex.quote(command)} "
        f"> {quoted}/stdout 2> {quoted}/stderr < /dev/null; "
        f"printf '%s' \"$?\" > {quoted}/status"
    )
    return "\n".join((
        "set -e",
        f"mkdir -p {quoted}",
        f"cd {shlex.quote(workdir)}",
        "set -m",
        f"nohup bash -c {shlex.quote(supervisor)} >/dev/null 2>&1 < /dev/null &",
        "child=$!",
        f"printf '%s' \"$child\" > {quoted}/pid",
        # The process start time, recorded so a recycled pid cannot be
        # mistaken for this job. A container restart resets pids to low
        # numbers, so a stale pid file very plausibly matches some unrelated
        # process - and a dead job would then read as 'running' forever.
        f"(sed 's/.*) //' /proc/$child/stat 2>/dev/null | awk '{{print $20}}' "
        f"|| true) > {quoted}/starttime",
        f"cat {quoted}/pid",
    ))


def job_probe_script(directory: str) -> str:
    """Shell that reports a job's pid, recorded status and liveness.

    Liveness requires both that the pid exists *and* that its start time still
    matches what was recorded at launch. Without the second check a recycled
    pid - routine after a container restart, where pids begin again from low
    numbers - makes a long-dead job look like it is still running.
    """
    quoted = shlex.quote(directory)
    return (
        f"pid=$(cat {quoted}/pid 2>/dev/null || true); "
        f"status=$(cat {quoted}/status 2>/dev/null || true); "
        f"want=$(cat {quoted}/starttime 2>/dev/null || true); "
        f"alive=no; "
        f'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=yes; fi; '
        # Only downgrade when both start times are actually known: an
        # unreadable /proc must not turn a live job into a lost one.
        f"have=$(sed 's/.*) //' /proc/$pid/stat 2>/dev/null | awk '{{print $20}}' || true); "
        f'if [ "$alive" = yes ] && [ -n "$want" ] && [ -n "$have" ] '
        f'&& [ "$want" != "$have" ]; then alive=no; fi; '
        f"out=$(wc -c < {quoted}/stdout 2>/dev/null || echo 0); "
        f"err=$(wc -c < {quoted}/stderr 2>/dev/null || echo 0); "
        f"printf 'pid=%s status=%s alive=%s stdout=%s stderr=%s' "
        f'"$pid" "$status" "$alive" "$out" "$err"'
    )


def job_cancel_script(directory: str) -> str:
    """Shell that kills a job's whole process group, then the leader.

    Same reasoning as the exec timeout path (commit 3612261): killing only the
    leader leaves grandchildren alive holding the output files open. The
    negative PID targets the process group that ``set -m`` created in
    ``job_launch_script`` for exactly this.
    """
    quoted = shlex.quote(directory)
    return (
        f"pid=$(cat {quoted}/pid 2>/dev/null || true); "
        f'if [ -z "$pid" ]; then exit 1; fi; '
        f'kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; '
        f"sleep 2; "
        f'kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true; '
        f"exit 0"
    )


def parse_job_probe(output: str) -> dict:
    """Turn ``job_probe_script`` output into a state dict.

    ``lost`` is deliberate: a pid that is gone with no recorded status means
    the container restarted or something outside killed the process, and
    reporting that as ``exited 0`` would hand the model a fabricated success.
    """
    fields = dict(
        item.split("=", 1) for item in (output or "").split() if "=" in item
    )
    status = fields.get("status", "").strip()
    alive = fields.get("alive") == "yes"
    if status:
        state = "exited"
    elif alive:
        state = "running"
    else:
        state = "lost"
    return {
        "state": state,
        "exit_code": int(status) if status.lstrip("-").isdigit() else None,
        "pid": int(fields["pid"]) if fields.get("pid", "").isdigit() else None,
        "stdout_bytes": int(fields.get("stdout", "0") or 0),
        "stderr_bytes": int(fields.get("stderr", "0") or 0),
    }


def task_container_name(project_id: str) -> str:
    return f"ipc-task-{_safe_segment(project_id)}"


def member_workdir(member: str) -> str:
    return SHARED_DIR


class TaskSandbox:

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
        # Named Linux volumes also work when the backend uses the host Docker
        # socket: no incorrect backend-to-host bind path translation is needed.
        self._volume_name = f"{self._container_name}-workspace"
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

            created = False
            try:
                try:
                    container = client.containers.get(self._container_name)
                    container.reload()
                    mounts = getattr(container, "attrs", {}).get("Mounts")
                    if mounts is not None and not any(m.get("Destination") == WORKSPACE for m in mounts):
                        raise DockerConfigurationError(
                            "legacy task container has no persistent workspace; export /workspace before recreating it",
                            operation="validate workspace persistence",
                        )
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
                        "volumes": {self._volume_name: {"bind": WORKSPACE, "mode": "rw"}},
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
                        created = True
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
                    if created and "container" in locals():
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

    # ---- detached jobs ----

    def exec_detached(self, job_id: str, command: str, workdir: str) -> int:
        """Start ``command`` with no timeout and return its process-group PID.

        No ``timeout`` wrapper: removing the 60s ceiling is the point. The
        durable handle is the PID and output files under ``job_dir``, not a
        docker exec id, because an exec id dies with a container restart while
        the workspace volume survives it.
        """
        if self._container is None:
            self.start()
        result = self._raw_exec(
            job_launch_script(job_dir(job_id), command, workdir),
            workdir=workdir,
            timeout=20,
        )
        if not result.ok:
            raise RuntimeError(
                f"failed to start detached job: {(result.stderr or result.stdout).strip()}"
            )
        try:
            return int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(
                f"detached job reported no pid: {result.stdout.strip()!r}"
            ) from exc

    def job_state(self, job_id: str) -> dict:
        """Report a detached job's liveness and exit status from its files."""
        result = self._raw_exec(
            job_probe_script(job_dir(job_id)), workdir=WORKSPACE, timeout=15
        )
        return parse_job_probe(result.stdout)

    def job_output(
        self, job_id: str, stream: str = "stdout", *, offset: int = 0,
        limit: int = 32_768,
    ) -> tuple[str, int]:
        """Read a job's output incrementally; returns ``(text, next_offset)``.

        Byte offsets rather than line counts so a partially written line cannot
        shift the cursor and duplicate output on the next poll.
        """
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        path = f"{job_dir(job_id)}/{stream}"
        quoted = shlex.quote(path)
        result = self._raw_exec(
            f"tail -c +{max(0, offset) + 1} {quoted} 2>/dev/null | head -c {max(1, limit)}",
            workdir=WORKSPACE,
            timeout=20,
        )
        text = result.stdout or ""
        return text, max(0, offset) + len(text.encode("utf-8", errors="replace"))

    def job_cancel(self, job_id: str) -> bool:
        """Kill a job's whole process group; see ``job_cancel_script``."""
        result = self._raw_exec(
            job_cancel_script(job_dir(job_id)), workdir=WORKSPACE, timeout=20
        )
        return result.ok


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

    # ---- detached jobs (see TaskSandbox for the durability reasoning) ----

    def exec_detached(self, job_id: str, command: str) -> int:
        return self._task.exec_detached(job_id, command, workdir=self.workdir)

    def job_state(self, job_id: str) -> dict:
        return self._task.job_state(job_id)

    def job_output(
        self, job_id: str, stream: str = "stdout", *, offset: int = 0,
        limit: int = 32_768,
    ) -> tuple[str, int]:
        return self._task.job_output(job_id, stream, offset=offset, limit=limit)

    def job_cancel(self, job_id: str) -> bool:
        return self._task.job_cancel(job_id)

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
