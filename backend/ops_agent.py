"""Internal, read-only Docker information service for the operations center."""

import os
import secrets
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query


DOCKER_SOCKET_PATH = os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
OPS_AGENT_TOKEN = os.environ.get("OPS_AGENT_TOKEN", "")
MAX_LOG_BYTES = 10 * 1024 * 1024


def _load_container_allowlist() -> dict[str, str]:
    result: dict[str, str] = {}
    raw = os.environ.get(
        "OPS_AGENT_CONTAINERS",
        "backend=binhu-backend,mysql=binhu-mysql",
    )
    for item in raw.split(","):
        source, separator, container = item.partition("=")
        if separator and source.strip() and container.strip():
            result[source.strip()] = container.strip()
    return result


ALLOWED_CONTAINERS = _load_container_allowlist()
app = FastAPI(title="Binhu read-only operations agent", docs_url=None, redoc_url=None)


async def require_agent_token(
    x_ops_agent_token: str = Header(default=""),
) -> None:
    if not OPS_AGENT_TOKEN or not secrets.compare_digest(
        x_ops_agent_token,
        OPS_AGENT_TOKEN,
    ):
        raise HTTPException(status_code=403, detail="forbidden")


def _container_name(source: str) -> str:
    container = ALLOWED_CONTAINERS.get(source)
    if not container:
        raise HTTPException(status_code=404, detail="unknown log source")
    return container


async def _docker_get(path: str, params: dict | None = None) -> httpx.Response:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET_PATH)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=20,
    ) as client:
        response = await client.get(path, params=params)
        response.raise_for_status()
        return response


def decode_docker_stream(payload: bytes) -> list[dict[str, str]]:
    """Decode Docker's multiplexed stdout/stderr frames, with plain-text fallback."""
    frames: list[tuple[str, bytes]] = []
    offset = 0
    while offset + 8 <= len(payload):
        header = payload[offset : offset + 8]
        if header[0] not in (0, 1, 2) or header[1:4] != b"\x00\x00\x00":
            break
        size = int.from_bytes(header[4:8], "big")
        if offset + 8 + size > len(payload):
            break
        stream = "stderr" if header[0] == 2 else "stdout"
        frames.append((stream, payload[offset + 8 : offset + 8 + size]))
        offset += 8 + size

    if not frames or offset != len(payload):
        frames = [("stdout", payload)]

    lines: list[dict[str, str]] = []
    for stream, data in frames:
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            lines.append({"stream": stream, "message": line})
    return lines


def _cpu_percent(stats: dict) -> float:
    current = stats.get("cpu_stats") or {}
    previous = stats.get("precpu_stats") or {}
    cpu_delta = (
        (current.get("cpu_usage") or {}).get("total_usage", 0)
        - (previous.get("cpu_usage") or {}).get("total_usage", 0)
    )
    system_delta = current.get("system_cpu_usage", 0) - previous.get(
        "system_cpu_usage",
        0,
    )
    cpu_count = current.get("online_cpus") or len(
        (current.get("cpu_usage") or {}).get("percpu_usage") or []
    )
    if cpu_delta <= 0 or system_delta <= 0 or cpu_count <= 0:
        return 0.0
    return round(cpu_delta / system_delta * cpu_count * 100, 2)


def _network_totals(stats: dict) -> tuple[int, int]:
    networks = stats.get("networks") or {}
    return (
        sum(item.get("rx_bytes", 0) for item in networks.values()),
        sum(item.get("tx_bytes", 0) for item in networks.values()),
    )


def _memory_totals(memory: dict) -> tuple[int, int]:
    """Return Docker-style working set and reclaimable file cache.

    Docker's API reports raw cgroup usage, which includes inactive file cache.
    The CLI subtracts that cache for its MEM USAGE value because Linux can
    reclaim it under pressure.  Use the same cgroup v1/v2 fallback here so the
    operations page does not present cache as application working memory.
    """
    usage = max(int(memory.get("usage") or 0), 0)
    stats = memory.get("stats") or {}
    inactive_file = stats.get("total_inactive_file")
    if inactive_file is None:
        inactive_file = stats.get("inactive_file", 0)
    reclaimable_cache = min(max(int(inactive_file or 0), 0), usage)
    return usage - reclaimable_cache, reclaimable_cache


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/internal/overview", dependencies=[Depends(require_agent_token)])
async def overview():
    containers = []
    for source, name in ALLOWED_CONTAINERS.items():
        encoded = quote(name, safe="")
        try:
            inspect = (await _docker_get(f"/containers/{encoded}/json")).json()
            stats = (
                await _docker_get(
                    f"/containers/{encoded}/stats",
                    {"stream": "false"},
                )
            ).json()
            state = inspect.get("State") or {}
            memory = stats.get("memory_stats") or {}
            memory_used, memory_cache = _memory_totals(memory)
            rx_bytes, tx_bytes = _network_totals(stats)
            containers.append(
                {
                    "source": source,
                    "name": name,
                    "image": (inspect.get("Config") or {}).get("Image", ""),
                    "status": state.get("Status", "unknown"),
                    "health": (state.get("Health") or {}).get("Status"),
                    "started_at": state.get("StartedAt"),
                    "restart_count": inspect.get("RestartCount", 0),
                    "cpu_percent": _cpu_percent(stats),
                    "memory_used_bytes": memory_used,
                    "memory_cache_bytes": memory_cache,
                    "memory_limit_bytes": memory.get("limit", 0),
                    "network_rx_bytes": rx_bytes,
                    "network_tx_bytes": tx_bytes,
                }
            )
        except Exception as exc:
            containers.append(
                {
                    "source": source,
                    "name": name,
                    "status": "unavailable",
                    "error": str(exc)[:200],
                }
            )
    return {"containers": containers}


@app.get("/internal/logs/{source}", dependencies=[Depends(require_agent_token)])
async def logs(
    source: str,
    tail: int = Query(default=300, ge=1, le=5000),
    since: int = Query(default=0, ge=0),
):
    name = quote(_container_name(source), safe="")
    response = await _docker_get(
        f"/containers/{name}/logs",
        {
            "stdout": "true",
            "stderr": "true",
            "timestamps": "true",
            "tail": str(tail),
            "since": str(since),
        },
    )
    lines = decode_docker_stream(response.content)
    encoded_size = 0
    limited_lines: list[dict[str, str]] = []
    for line in reversed(lines):
        line_size = len(line["message"].encode("utf-8", errors="replace"))
        if limited_lines and encoded_size + line_size > MAX_LOG_BYTES:
            break
        encoded_size += line_size
        limited_lines.append(line)
    limited_lines.reverse()
    return {
        "source": source,
        "truncated": len(response.content) > MAX_LOG_BYTES,
        "lines": limited_lines,
    }
