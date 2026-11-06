from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from backend.platform.downloads import stage_challenges, stage_challenges_reporting


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


def test_one_unreachable_attachment_keeps_the_rest_of_the_import(tmp_path):
    """A 401 from one attachment host must not discard the whole challenge set."""

    def download(challenge, destination):
        if challenge.external_id == "bad":
            raise RuntimeError("401 from the attachment host")
        destination.mkdir(parents=True)
        path = destination / "ok.txt"
        path.write_text("x")
        return [path]

    adapter = SimpleNamespace(download_attachments=download)
    challenges = [
        SimpleNamespace(external_id="good", attachment_urls=["https://ctf.example/a.zip"]),
        SimpleNamespace(external_id="bad", attachment_urls=["https://ctf.example/b.zip"]),
    ]
    staged, failures = stage_challenges_reporting(
        lambda: adapter, challenges, tmp_path / "staging"
    )

    assert set(staged) == {"good"}
    assert "401" in str(failures["bad"])
    # the strict entry point keeps its all-or-nothing contract
    with pytest.raises(RuntimeError, match="401"):
        stage_challenges(lambda: adapter, challenges, tmp_path / "strict")


def test_bad_adapter_cannot_publish_outside_staging(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("x")
    adapter = SimpleNamespace(download_attachments=lambda c, d: [outside])
    with pytest.raises(ValueError, match="outside"):
        stage_challenges(lambda: adapter, [SimpleNamespace(external_id="1", attachment_urls=[])], tmp_path / "staging")
