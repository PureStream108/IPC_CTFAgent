from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.orchestrator import Orchestrator


class _Member:
    def __init__(self) -> None:
        self.stopped = threading.Event()

    def stop(self) -> None:
        self.stopped.set()


def test_shutdown_stops_members_and_waits_for_running_workers() -> None:
    orchestrator = object.__new__(Orchestrator)
    orchestrator._stop = threading.Event()
    orchestrator._loop_thread = None
    orchestrator._lock = threading.Lock()
    member = _Member()
    orchestrator._members = {"proj_001": {"member": member}}
    orchestrator.startup_executor = ThreadPoolExecutor(max_workers=1)
    orchestrator.executor = ThreadPoolExecutor(max_workers=1)

    startup_started = threading.Event()
    startup_release = threading.Event()
    member_started = threading.Event()
    member_release = threading.Event()

    def block(started: threading.Event, release: threading.Event) -> None:
        started.set()
        release.wait()

    shutdown_thread = None
    try:
        orchestrator.startup_executor.submit(block, startup_started, startup_release)
        orchestrator.executor.submit(block, member_started, member_release)
        assert startup_started.wait(timeout=1)
        assert member_started.wait(timeout=1)

        shutdown_thread = threading.Thread(target=orchestrator.shutdown)
        shutdown_thread.start()

        assert member.stopped.wait(timeout=1)
        assert orchestrator._stop.is_set()
        assert shutdown_thread.is_alive()

        startup_release.set()
        shutdown_thread.join(timeout=0.1)
        assert shutdown_thread.is_alive()

        member_release.set()
        shutdown_thread.join(timeout=1)
        assert not shutdown_thread.is_alive()
    finally:
        startup_release.set()
        member_release.set()
        if shutdown_thread is not None and shutdown_thread.is_alive():
            shutdown_thread.join(timeout=2)
        else:
            orchestrator.startup_executor.shutdown(wait=True, cancel_futures=True)
            orchestrator.executor.shutdown(wait=True, cancel_futures=True)

    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        orchestrator.startup_executor.submit(lambda: None)
    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        orchestrator.executor.submit(lambda: None)
