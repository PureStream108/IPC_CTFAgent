from __future__ import annotations

import asyncio
import faulthandler
import os
import threading
import time
from contextlib import suppress
from pathlib import Path


class RuntimeTelemetry:
    def __init__(self, root: Path, *, stall_seconds: float = 10):
        self.root = root
        self.stall_seconds = stall_seconds
        self.last_heartbeat = time.monotonic()
        self.loop_lag_seconds = 0.0
        self.max_loop_lag_seconds = 0.0
        self._done = threading.Event()
        self._task = None
        self._thread = None

    def start(self):
        self.last_heartbeat = time.monotonic()
        self._task = asyncio.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name="ipc-loop-diagnostics", daemon=True)
        self._thread.start()

    async def _heartbeat(self):
        while not self._done.is_set():
            before = time.monotonic()
            await asyncio.sleep(.5)
            now = time.monotonic()
            self.loop_lag_seconds = max(0, now - before - .5)
            self.max_loop_lag_seconds = max(self.max_loop_lag_seconds, self.loop_lag_seconds)
            self.last_heartbeat = now

    def _watch(self):
        last_dump = 0.0
        while not self._done.wait(.5):
            now = time.monotonic()
            if now-self.last_heartbeat >= self.stall_seconds and now-last_dump >= 60:
                last_dump = now
                directory = self.root / "logs" / "runtime"
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "event-loop-stalls.log").open("ab") as stream:
                    stream.write(f"pid={os.getpid()} monotonic={now:.6f} stalled_seconds={now-self.last_heartbeat:.3f}\n".encode())
                    stream.flush()
                    faulthandler.dump_traceback(file=stream, all_threads=True)

    def snapshot(self, state=None) -> dict:
        result = {"pid": os.getpid(), "threads": threading.active_count(),
                  "cpu_seconds": round(time.process_time(), 6),
                  "event_loop_lag_seconds": round(self.loop_lag_seconds, 6),
                  "max_event_loop_lag_seconds": round(self.max_loop_lag_seconds, 6),
                  "heartbeat_age_seconds": round(time.monotonic()-self.last_heartbeat, 6)}
        try:
            import resource
            result["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            result["peak_rss_kib"] = None
        orchestrator = getattr(state, "orchestrator", None)
        for name in ("executor", "startup_executor"):
            queue = getattr(getattr(orchestrator, name, None), "_work_queue", None)
            if queue is not None:
                result[f"{name}_queued"] = queue.qsize()
        competition = getattr(state, "competition", None)
        result["competition_phases"] = dict(getattr(competition, "phase_metrics", {}))
        return result

    async def close(self):
        self._done.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._thread:
            self._thread.join(timeout=1)
