from __future__ import annotations

"""S2: a sandbox exec must never run on the event loop.

TaskSandbox.exec and exec_bounded end in Container.exec_run, a blocking HTTP
round trip. Before the async surface existed, a Member running a 60 second bash
froze the loop that also serves API requests, WebSocket pushes and every other
Member's model stream. These tests pin the thread hop that fixes it.

The blocking work is faked rather than executed, so the test measures scheduling
and not Docker.
"""

import asyncio
import itertools
import time

import pytest

from backend.sandbox.sandbox import ExecResult
from backend.sandbox.task_sandbox import MemberSandbox, TaskSandbox


class FakeTaskSandbox(TaskSandbox):
    """TaskSandbox with a container side that sleeps for selected commands."""

    def __init__(self, delays: dict[str, float] | None = None):
        super().__init__(project_id="proj_concurrency", image="ipc-task:latest")
        self.delays = dict(delays or {})
        self.commands: list[str] = []

    def start(self) -> None:
        if self._container is None:
            self._container = object()

    def exec_bounded(self, command: str, workdir: str, timeout: int = 60) -> ExecResult:
        self.commands.append(command)
        delay = self.delays.get(command, 0.0)
        if delay:
            time.sleep(delay)
        return ExecResult(0, command, "", False, {}, delay)

    def read_page(self, path: str, *, workdir: str, offset: int = 0, limit: int = 8192) -> dict:
        return {"path": path, "offset": offset, "limit": limit, "data": "page"}

    def job_state(self, job_id: str) -> dict:
        return {"state": "running", "pid": 1, "job_id": job_id}


def test_a_long_command_does_not_stall_the_event_loop():
    task = FakeTaskSandbox({"sleep 20": 1.0})
    amber = MemberSandbox(task, "amber", "/workspace/shared")
    jade = MemberSandbox(task, "jade", "/workspace/shared")

    async def scenario():
        ticks: list[float] = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        beat = asyncio.create_task(heartbeat())
        slow = asyncio.create_task(amber.aexec("sleep 20", timeout=60))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        fast = await jade.aexec("echo ok", timeout=60)
        fast_elapsed = time.monotonic() - started
        slow_result = await slow
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        gaps = [b - a for a, b in itertools.pairwise(ticks)]
        return fast, fast_elapsed, slow_result, gaps

    fast, fast_elapsed, slow_result, gaps = asyncio.run(scenario())

    assert fast.stdout == "echo ok"
    assert fast_elapsed < 0.5
    assert slow_result.stdout == "sleep 20"
    assert gaps, "the heartbeat never ran"
    assert max(gaps) < 0.2, f"event loop stalled for {max(gaps):.3f}s"


def test_the_async_surface_delegates_every_blocking_call():
    task = FakeTaskSandbox()
    sandbox = MemberSandbox(task, "topaz", "/workspace/shared")

    async def scenario():
        page = await sandbox.aread_page("artifact.log", offset=7, limit=16)
        state = await sandbox.ajob_state("job-1")
        return page, state

    page, state = asyncio.run(scenario())

    assert page == {
        "path": "artifact.log", "offset": 7, "limit": 16, "data": "page",
    }
    assert state["job_id"] == "job-1"


def test_raw_exec_refuses_an_absent_container():
    """A vanished container is a RuntimeError, not an AttributeError.

    BaseMember._container_gone classifies this message, so the exec path has to
    keep raising it instead of dereferencing None.
    """

    task = TaskSandbox(project_id="proj_gone", image="ipc-task:latest")

    with pytest.raises(RuntimeError, match="task container is not running"):
        task._raw_exec("id", workdir="/workspace/shared")
