from __future__ import annotations


import importlib
import sys
from pathlib import Path

from backend.sandbox.errors import DockerConfigurationError


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
        candidates.extend(Path(entry) for entry in module_paths)

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
    except Exception as exc:
        raise DockerConfigurationError(
            f"Docker SDK import failed: {type(exc).__name__}: {exc}",
            operation="load Docker SDK",
        ) from exc
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
        raise DockerConfigurationError(
            "Docker sandbox imported a non-SDK `docker` module "
            f"({origin}). Install the Python Docker SDK and run from an environment "
            "where it is not shadowed by a local docker/ directory.",
            operation="load Docker SDK",
        )
    except ImportError as exc:
        raise DockerConfigurationError(
            "Docker sandbox requires the Python Docker SDK. Install with `pip install -e .[docker]`.",
            operation="load Docker SDK",
        ) from (initial_import_error or exc)
    except DockerConfigurationError:
        raise
    except Exception as exc:
        raise DockerConfigurationError(
            f"Docker SDK import failed: {type(exc).__name__}: {exc}",
            operation="load Docker SDK",
        ) from exc
    finally:
        sys.path[:] = saved_path
        if not loaded_sdk:
            if previous_module is not None:
                sys.modules["docker"] = previous_module
            else:
                sys.modules.pop("docker", None)
