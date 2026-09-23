from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from backend.platform.downloads import stage_challenges


def test_downloads_parallel_with_per_origin_bound_and_safe_names(tmp_path):
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    active = 0
    peak = 0

    class Adapter:
        def download_attachments(self, challenge, destination):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            destination.mkdir(parents=True)
            path = destination / "file.txt"
            path.write_text(challenge.external_id)
            with lock:
                active -= 1
            return [path]

    challenges = [SimpleNamespace(external_id=f"../../{i}", attachment_urls=["https://ctf.example/file"]) for i in range(4)]
    files = stage_challenges(Adapter, challenges, tmp_path, workers=4)
    assert peak == 2
    assert len(files) == 4
    assert all(p.is_relative_to(tmp_path) for group in files.values() for p in group)


def test_bad_adapter_cannot_publish_outside_staging(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("x")
    adapter = SimpleNamespace(download_attachments=lambda c, d: [outside])
    with pytest.raises(ValueError, match="outside"):
        stage_challenges(lambda: adapter, [SimpleNamespace(external_id="1", attachment_urls=[])], tmp_path / "staging")
