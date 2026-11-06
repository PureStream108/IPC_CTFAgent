from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit


def stage_challenges(
    adapter_factory: Callable, challenges: list, destination: Path, *, workers: int = 4
) -> dict[str, list[Path]]:
    """Download every challenge's attachments, failing the batch on the first error."""

    staged, failures = stage_challenges_reporting(
        adapter_factory, challenges, destination, workers=workers
    )
    if failures:
        raise next(iter(failures.values()))
    return staged


def stage_challenges_reporting(
    adapter_factory: Callable, challenges: list, destination: Path, *, workers: int = 4
) -> tuple[dict[str, list[Path]], dict[str, Exception]]:
    """Download attachments, keeping a challenge whose download failed.

    Live platforms serve attachments through endpoints that need a session
    cookie, a signed query or an allow-listed client, so a single 401 used to
    abort the whole import and leave the run blocked with no projects at all.
    The challenge itself is still worth working from its description, so the
    failure is reported per challenge and the caller decides what to tell the
    operator.  A path that escapes its staging directory stays fatal: that is a
    contract violation, not a flaky download.
    """
    if not 1 <= workers <= 16:
        raise ValueError("download workers must be between 1 and 16")
    ids = [c.external_id for c in challenges]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate challenge identity in platform response")
    destination.mkdir(parents=True, exist_ok=True)
    origins = {
        c.external_id: sorted({urlsplit(url).netloc.lower() for url in c.attachment_urls} or {"platform"})
        for c in challenges
    }
    gates = {origin: BoundedSemaphore(2) for group in origins.values() for origin in group}

    def download(item):
        index, challenge = item
        directory = destination / str(index)
        adapter = adapter_factory()
        with ExitStack() as stack:
            for origin in origins[challenge.external_id]:
                stack.enter_context(gates[origin])
            files = adapter.download_attachments(challenge, directory)
        for file in files:
            if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
                raise ValueError("adapter returned a file outside its staging directory")
        return challenge.external_id, files

    staged: dict[str, list[Path]] = {}
    failures: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ipc-download") as pool:
        futures = {
            pool.submit(download, item): item[1] for item in enumerate(challenges)
        }
        for future, challenge in futures.items():
            try:
                external_id, files = future.result()
            except ValueError as exc:
                if "outside its staging directory" in str(exc):
                    raise
                failures[str(challenge.external_id)] = exc
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures[str(challenge.external_id)] = exc
            else:
                staged[external_id] = files
    return staged, failures
