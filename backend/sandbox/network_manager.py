from __future__ import annotations


import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChallengeEnv:
    project_id: str
    compose_file: str | None = None
    dockerfile: str | None = None
    network_name: str = ""
    started: bool = False
    endpoints: list[str] = field(default_factory=list)


class NetworkManager:
    def __init__(self, backend: str = "local"):
        self.backend = backend
        self._envs: dict[str, ChallengeEnv] = {}
        self._lock = threading.Lock()

    def detect(self, attachments_dir: str | Path) -> ChallengeEnv | None:
        """Look for a Dockerfile / docker-compose.* in the attachments."""
        d = Path(attachments_dir)
        if not d.exists():
            return None
        compose = None
        dockerfile = None
        for pat in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
            candidate = d / pat
            if candidate.exists():
                compose = str(candidate)
                break
        df = d / "Dockerfile"
        if df.exists():
            dockerfile = str(df)
        if compose is None and dockerfile is None:
            return None
        return ChallengeEnv(project_id="", compose_file=compose, dockerfile=dockerfile)

    def start(self, project_id: str, attachments_dir: str | Path) -> ChallengeEnv | None:
        env = self.detect(attachments_dir)
        if env is None:
            return None
        env.project_id = project_id
        env.network_name = f"ipc-proj-{project_id}"
        if self.backend == "docker":
            env.started = self._docker_up(env)
        else:
            env.started = True  # local/test: record only
        with self._lock:
            self._envs[project_id] = env
        return env

    def _docker_up(self, env: ChallengeEnv) -> bool:  # pragma: no cover - needs docker
        import subprocess

        try:
            if env.compose_file:
                subprocess.run(
                    ["docker", "compose", "-p", env.network_name, "-f", env.compose_file, "up", "-d"],
                    check=True,
                    capture_output=True,
                )
            elif env.dockerfile:
                img = f"{env.network_name}-img"
                ctx = str(Path(env.dockerfile).parent)
                subprocess.run(["docker", "build", "-t", img, ctx], check=True, capture_output=True)
                subprocess.run(
                    ["docker", "run", "-d", "--name", env.network_name, img],
                    check=True,
                    capture_output=True,
                )
            return True
        except Exception:
            return False

    def get(self, project_id: str) -> ChallengeEnv | None:
        with self._lock:
            return self._envs.get(project_id)

    def stop(self, project_id: str) -> None:
        with self._lock:
            env = self._envs.pop(project_id, None)
        if env is None or self.backend != "docker":
            return
        self._docker_down(env)

    def _docker_down(self, env: ChallengeEnv) -> None:  # pragma: no cover - needs docker
        import subprocess

        try:
            if env.compose_file:
                subprocess.run(
                    ["docker", "compose", "-p", env.network_name, "-f", env.compose_file, "down"],
                    check=False,
                    capture_output=True,
                )
            else:
                subprocess.run(["docker", "rm", "-f", env.network_name], check=False, capture_output=True)
        except Exception:
            pass
