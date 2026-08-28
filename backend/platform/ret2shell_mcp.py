from __future__ import annotations

import asyncio
from typing import Any

from backend.mcp.mcp_server import MCPServer, close_lifespan, create_mcp_server
from backend.platform.ret2shell import Ret2ShellClient


def build_ret2shell_mcp(client: Ret2ShellClient) -> MCPServer:
    """Expose ret2shell dynamic-instance control to Members.

    Credentials stay in the backend process; Members only ever see instance
    endpoints and challenge state.  Flag submission is deliberately NOT
    exposed here — the shared 10-per-5-minutes quota is spent only by the
    operator through scripts/ret2shell_submit.py.
    """

    server = create_mcp_server(
        "ret2shell",
        "ret2shell competition platform: dynamic challenge instances and solve state",
        lifespan=close_lifespan(client.close),
    )

    def _endpoint(instance: dict[str, Any]) -> Any:
        return instance.get("exposed_ports")

    @server.tool(
        name="instance_start",
        description=(
            "Start (or reuse) the dynamic instance for one challenge and wait "
            "until its endpoints are reachable. Returns the exposed address "
            "and ports to attack. Requires the challenge id."
        ),
    )
    async def instance_start(challenge_id: int) -> dict[str, Any]:
        existing = await asyncio.to_thread(client.find_instance, challenge_id)
        if existing is None:
            await asyncio.to_thread(client.start_instance, challenge_id)
        instance = await asyncio.to_thread(client.wait_for_instance, challenge_id)
        return {
            "challenge_id": challenge_id,
            "state": instance.get("state"),
            "endpoints": _endpoint(instance),
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
        return {
            "challenge_id": challenge_id,
            "running": instance.get("state") == "Running",
            "state": instance.get("state"),
            "endpoints": _endpoint(instance),
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
        await asyncio.to_thread(client.destroy_instance, challenge_id)
        return {"challenge_id": challenge_id, "stopped": True}

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
