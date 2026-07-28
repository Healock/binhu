"""Client for the internal read-only operations agent."""

from typing import Any

import httpx

from config import settings


def _headers() -> dict[str, str]:
    return {"X-Ops-Agent-Token": settings.OPS_AGENT_TOKEN}


async def get_container_overview() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{settings.OPS_AGENT_URL.rstrip('/')}/internal/overview",
            headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def get_container_logs(
    source: str,
    *,
    tail: int = 300,
    since: int = 0,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{settings.OPS_AGENT_URL.rstrip('/')}/internal/logs/{source}",
            headers=_headers(),
            params={"tail": tail, "since": since},
        )
        response.raise_for_status()
        return response.json()
