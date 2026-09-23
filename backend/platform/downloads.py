"""Bounded parallel staging with a fresh adapter for each challenge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import BoundedSemaphore
from typing import Callable
from urllib.parse import urlsplit


def stage_challenges(adapter_factory: Callable, challenges: list, destination: Path, *, workers: int = 4) -> dict[str, list[Path]]:
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
        # Platform IDs are not paths. Never interpolate them into filenames.
        directory = destination / str(index)
        adapter = adapter_factory()
        # Acquire in a global order for multi-origin challenges to avoid cycles.
        with ExitStack() as stack:
            for origin in origins[challenge.external_id]:
                stack.enter_context(gates[origin])
            files = adapter.download_attachments(challenge, directory)
        for file in files:
            if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
                raise ValueError("adapter returned a file outside its staging directory")
        return challenge.external_id, files

    # executor.map keeps results deterministic and waits for running jobs on
    # failure, so callers can safely remove staging after this function exits.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ipc-download") as pool:
        return dict(pool.map(download, enumerate(challenges)))
