from __future__ import annotations

from typing import TypeAlias


class SandboxStartupError(RuntimeError):
    """Infrastructure failure raised while preparing an isolated solver runtime."""

    kind = "sandbox"

    def __init__(self, message: str, *, operation: str | None = None):
        self.operation = operation
        super().__init__(message)


class DockerStartupError(SandboxStartupError):
    kind = "docker"


class DockerDaemonError(DockerStartupError):
    kind = "docker_daemon"


class DockerSocketError(DockerStartupError):
    kind = "docker_socket"


class DockerImageError(DockerStartupError):
    kind = "docker_image"


class DockerNetworkError(DockerStartupError):
    kind = "docker_network"


class DockerConfigurationError(DockerStartupError):
    kind = "docker_config"


DockerErrorType: TypeAlias = type[DockerStartupError]


def classify_docker_startup_error(
    error: BaseException,
    operation: str,
    *,
    default: DockerErrorType = DockerConfigurationError,
) -> DockerStartupError:
    """Convert Docker SDK/CLI errors into stable infrastructure categories."""

    if isinstance(error, DockerStartupError):
        return error

    detail = str(error).strip() or type(error).__name__
    text = f"{type(error).__name__}: {detail}".lower()

    error_type: DockerErrorType
    if (
        "imagenotfound" in text
        or "no such image" in text
        or "manifest unknown" in text
        or "pull access denied" in text
        or "repository does not exist" in text
    ):
        error_type = DockerImageError
    elif (
        "docker.sock" in text
        or "docker_engine" in text
        or "named pipe" in text
        or "createfile" in text
        or "filenotfounderror" in text
        or "permission denied" in text and ("socket" in text or "pipe" in text)
    ):
        error_type = DockerSocketError
    elif (
        "cannot connect to the docker daemon" in text
        or "is the docker daemon running" in text
        or "error while fetching server api version" in text
        or "connection refused" in text
        or "connection aborted" in text
        or "connection reset" in text
        or "max retries exceeded" in text
    ):
        error_type = DockerDaemonError
    elif "network" in text and any(
        marker in text
        for marker in (
            "not found",
            "failed",
            "could not",
            "cannot",
            "address pool",
            "endpoint",
            "iptables",
        )
    ):
        error_type = DockerNetworkError
    else:
        error_type = default

    return error_type(f"{operation}: {detail}", operation=operation)


def classify_docker_cli_error(detail: str, operation: str) -> DockerStartupError:
    return classify_docker_startup_error(RuntimeError(detail), operation)
