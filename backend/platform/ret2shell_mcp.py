from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from backend.mcp.mcp_server import MCPServer, close_lifespan, create_mcp_server
from backend.platform.ret2shell import Ret2ShellClient

# ret2shell does not expose direct host:port pairs: each running instance
# carries a ``traffic`` token plus the container ``ports``, and the platform
# web app connects through wsrx (WebSocket Reflector X, XDSEC) links of the
# form ``wss://<platform>/api/traffic/<token>?port=<port>``.  The backend
# keeps one ``wsrx connect`` subprocess per (challenge, port) alive so that
# Members — running in their own containers on the same docker network — can
# simply connect to ``ipc-app:<local port>``.
WSRX_BINARY = os.getenv("IPC_R2S_WSRX_BINARY", "wsrx")
WSRX_BIND_HOST = os.getenv("IPC_R2S_WSRX_BIND_HOST", "0.0.0.0")
WSRX_ENDPOINT_HOST = os.getenv("IPC_R2S_WSRX_ENDPOINT_HOST", "ipc-app")
WSRX_BASE_PORT = int(os.getenv("IPC_R2S_WSRX_BASE_PORT", "20000"))
WSRX_PORT_SPAN = 10000
WSRX_STARTUP_TIMEOUT = 15.0
WSRX_STARTUP_INTERVAL = 0.3


def local_wsrx_port(challenge_id: int, index: int = 0) -> int:
    """Deterministic local port for a challenge's nth tunneled port."""

    return WSRX_BASE_PORT + (challenge_id % WSRX_PORT_SPAN) + index


def _platform_host(client: Ret2ShellClient) -> str:
    return urlparse(client.base_url).netloc


def _ws_scheme(client: Ret2ShellClient) -> str:
    return "wss" if urlparse(client.base_url).scheme == "https" else "ws"


def wsrx_remotes(instance: dict[str, Any], client: Ret2ShellClient) -> list[str]:
    """wsrx websocket URLs for one instance.

    Handles both shapes: explicit ``ws://``/``wss://`` entries inside
    ``exposed_ports`` (GZCTF-style platform proxy) and the ret2shell shape
    where the instance carries a ``traffic`` token plus ``ports``.
    """

    remotes: list[str] = []
    exposed = instance.get("exposed_ports")
    if isinstance(exposed, list):
        for entry in exposed:
            if isinstance(entry, str) and entry.startswith(("ws://", "wss://")):
                remotes.append(entry)
    traffic = instance.get("traffic")
    ports = instance.get("ports")
    if traffic and isinstance(ports, list):
        scheme = _ws_scheme(client)
        host = _platform_host(client)
        for port in ports:
            remotes.append(f"{scheme}://{host}/api/traffic/{traffic}?port={port}")
    return remotes


class _Tunnel:
    def __init__(self, remote: str, local_port: int, process: Any) -> None:
        self.remote = remote
        self.local_port = local_port
        self.process = process

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


class WsrxTunnelManager:
    """Own the ``wsrx connect`` subprocesses, keyed by challenge id.

    ``ensure`` is idempotent: a challenge whose tunnels are still running for
    the same set of remotes is reused instead of spawning duplicates.  The
    spawner/probe are injectable so tests can run without the wsrx binary.
    """

    def __init__(
        self,
        *,
        binary: str = WSRX_BINARY,
        bind_host: str = WSRX_BIND_HOST,
        endpoint_host: str = WSRX_ENDPOINT_HOST,
        spawner: Callable[..., Any] | None = None,
        probe: Callable[[int], bool] | None = None,
        startup_timeout: float = WSRX_STARTUP_TIMEOUT,
        startup_interval: float = WSRX_STARTUP_INTERVAL,
        terminator: Callable[[Any], None] | None = None,
    ) -> None:
        self.binary = binary
        self.bind_host = bind_host
        self.endpoint_host = endpoint_host
        self.startup_timeout = startup_timeout
        self.startup_interval = startup_interval
        self._spawner = spawner or self._spawn_process
        self._probe = probe or self._probe_port
        self._terminator = terminator or self._terminate_process
        self._lock = threading.Lock()
        self._tunnels: dict[int, list[_Tunnel]] = {}

    # ---- default subprocess plumbing ----

    def _spawn_process(self, remote: str, local_port: int) -> Any:
        binary = shutil.which(self.binary)
        if binary is None:
            raise RuntimeError(
                f"wsrx binary {self.binary!r} not found on PATH; cannot tunnel {remote}"
            )
        return subprocess.Popen(
            [
                binary,
                "connect",
                "--host",
                self.bind_host,
                "--port",
                str(local_port),
                remote,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _probe_port(local_port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=1.0):
                return True
        except OSError:
            return False

    @staticmethod
    def _terminate_process(process: Any) -> None:
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
        except Exception:
            pass

    # ---- public API ----

    def _alive(self, challenge_id: int) -> list[_Tunnel]:
        tunnels = [t for t in self._tunnels.get(challenge_id, []) if t.alive]
        self._tunnels[challenge_id] = tunnels
        return tunnels

    def ensure(self, challenge_id: int, remotes: list[str]) -> dict[str, Any]:
        """Guarantee one live tunnel per remote; return local endpoints.

        Returns ``{"endpoints": [...], "started": [...]}`` where endpoints are
        ``<endpoint_host>:<port>`` strings reachable from Member containers.
        Raises RuntimeError when a tunnel cannot be established.
        """

        with self._lock:
            existing = self._alive(challenge_id)
            needed = list(remotes)
            tunnels: list[_Tunnel] = []
            for tunnel in existing:
                if tunnel.remote in needed:
                    needed.remove(tunnel.remote)
                    tunnels.append(tunnel)
                else:
                    self._terminator(tunnel.process)
            started: list[str] = []
            for remote in needed:
                local_port = local_wsrx_port(challenge_id, remotes.index(remote))
                process = self._spawner(remote, local_port)
                tunnels.append(_Tunnel(remote, local_port, process))
                started.append(remote)
            self._tunnels[challenge_id] = tunnels
        # Probe outside the lock, on a snapshot, so a slow platform-side
        # websocket handshake never blocks other challenges' tool calls.
        deadline = time.monotonic() + self.startup_timeout
        for tunnel in tunnels:
            while time.monotonic() < deadline:
                if not tunnel.alive:
                    raise RuntimeError(
                        f"wsrx tunnel for {tunnel.remote} exited immediately "
                        f"(code {tunnel.process.returncode}); is wsrx installed?"
                    )
                if self._probe(tunnel.local_port):
                    break
                time.sleep(self.startup_interval)
            else:
                raise RuntimeError(
                    f"wsrx tunnel port {tunnel.local_port} did not become "
                    f"reachable within {self.startup_timeout:.0f}s"
                )
        return {
            "endpoints": [
                f"{self.endpoint_host}:{tunnel.local_port}"
                for tunnel in sorted(tunnels, key=lambda t: t.local_port)
            ],
            "started": started,
        }

    def stop(self, challenge_id: int) -> list[int]:
        """Terminate every tunnel subprocess of one challenge."""

        with self._lock:
            tunnels = self._tunnels.pop(challenge_id, [])
        for tunnel in tunnels:
            self._terminator(tunnel.process)
        return [tunnel.local_port for tunnel in tunnels]

    def endpoints(self, challenge_id: int) -> list[str]:
        with self._lock:
            tunnels = self._alive(challenge_id)
        return [
            f"{self.endpoint_host}:{tunnel.local_port}"
            for tunnel in sorted(tunnels, key=lambda t: t.local_port)
        ]

    def stop_all(self) -> None:
        with self._lock:
            challenge_ids = list(self._tunnels)
        for challenge_id in challenge_ids:
            self.stop(challenge_id)


def build_ret2shell_mcp(
    client: Ret2ShellClient, *, tunnel_manager: WsrxTunnelManager | None = None
) -> MCPServer:
    """Expose ret2shell dynamic-instance control to Members.

    Credentials stay in the backend process; Members only ever see instance
    endpoints and challenge state.  Flag submission is deliberately NOT
    exposed here — the shared 10-per-5-minutes quota is spent only by the
    operator through scripts/ret2shell_submit.py.
    """

    manager = tunnel_manager or WsrxTunnelManager()

    server = create_mcp_server(
        name="ret2shell",
        description=(
            "ret2shell competition platform: dynamic challenge instances and "
            "solve state"
        ),
        lifespan=close_lifespan(client.close),
    )

    def _direct_endpoints(instance: dict[str, Any]) -> Any:
        # Direct host:port style exposure (no wsrx needed): pass through.
        exposed = instance.get("exposed_ports")
        if isinstance(exposed, list) and exposed and not wsrx_remotes(instance, client):
            return exposed
        return None

    async def _resolve_endpoints(challenge_id: int, instance: dict[str, Any]) -> dict[str, Any]:
        remotes = wsrx_remotes(instance, client)
        if not remotes:
            return {"endpoints": _direct_endpoints(instance)}
        result = await asyncio.to_thread(manager.ensure, challenge_id, remotes)
        return {
            "endpoints": result["endpoints"],
            "wsrx_remotes": remotes,
            "tunnels_started": result["started"],
        }

    @server.tool(
        name="instance_start",
        description=(
            "Start (or reuse) the dynamic instance for one challenge and wait "
            "until its endpoints are reachable. wsrx-proxied instances are "
            "tunneled automatically: connect to the returned local "
            "host:port pairs with netcat/pwntools. Requires the challenge id."
        ),
    )
    async def instance_start(challenge_id: int) -> dict[str, Any]:
        existing = await asyncio.to_thread(client.find_instance, challenge_id)
        if existing is None:
            await asyncio.to_thread(client.start_instance, challenge_id)
        instance = await asyncio.to_thread(client.wait_for_instance, challenge_id)
        resolved = await _resolve_endpoints(challenge_id, instance)
        return {
            "challenge_id": challenge_id,
            "state": instance.get("state"),
            **resolved,
            "renew_count": instance.get("renew_count"),
        }

    @server.tool(
        name="instance_status",
        description=(
            "Report the current instance state and endpoints for one challenge, "
            "or null when no instance is running."
        ),
    )
    async def instance_status(challenge_id: int) -> dict[str, Any]:
        instance = await asyncio.to_thread(client.find_instance, challenge_id)
        if instance is None:
            return {"challenge_id": challenge_id, "running": False}
        remotes = wsrx_remotes(instance, client)
        if remotes:
            endpoints = await asyncio.to_thread(manager.endpoints, challenge_id)
            resolved = {"endpoints": endpoints, "wsrx_remotes": remotes}
        else:
            resolved = {"endpoints": _direct_endpoints(instance)}
        return {
            "challenge_id": challenge_id,
            "running": instance.get("state") == "Running",
            "state": instance.get("state"),
            **resolved,
            "renew_count": instance.get("renew_count"),
        }

    @server.tool(
        name="instance_renew",
        description="Extend the lifetime of the running instance for one challenge.",
    )
    async def instance_renew(challenge_id: int) -> dict[str, Any]:
        await asyncio.to_thread(client.renew_instance, challenge_id)
        return {"challenge_id": challenge_id, "renewed": True}

    @server.tool(
        name="instance_stop",
        description="Destroy the running instance for one challenge.",
    )
    async def instance_stop(challenge_id: int) -> dict[str, Any]:
        torn_down = await asyncio.to_thread(manager.stop, challenge_id)
        await asyncio.to_thread(client.destroy_instance, challenge_id)
        return {"challenge_id": challenge_id, "stopped": True, "tunnels_closed": torn_down}

    @server.tool(
        name="challenge_status",
        description=(
            "Report whether the team already solved one challenge and how many "
            "teams solved it overall. Check before planning around a challenge."
        ),
    )
    async def challenge_status(challenge_id: int) -> dict[str, Any]:
        status = await asyncio.to_thread(client.challenge_status, challenge_id)
        if not isinstance(status, dict):
            return {"challenge_id": challenge_id, "error": "unexpected status payload"}
        return {"challenge_id": challenge_id, **status}

    return server
