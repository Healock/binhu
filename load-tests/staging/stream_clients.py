"""Long-lived Staging SSE and Query WebSocket clients.

These clients only read events and send the documented resume frame.  They
never replay a business write after a disconnect.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlparse

from .workload import retry_delay


@dataclass(frozen=True)
class StreamHooks:
    opened: Callable[[str, bool, float], None]
    closed: Callable[[str], None]
    reconnecting: Callable[[str, int, float], None]
    failed: Callable[[str, str], None]


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in sorted(cookies.items()) if key and value)


def websocket_url(base_url: str, api_prefix: str, parser_type: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.path not in ("", "/"):
        raise ValueError("Staging WebSocket base URL must be a HTTPS origin")
    return f"wss://{parsed.netloc}{api_prefix}/query/live/{quote(parser_type, safe='')}"


def _delay(stop, seconds: float) -> bool:
    return bool(stop.wait(timeout=max(0.0, seconds)))


def run_sse(
    *,
    base_url: str,
    api_prefix: str,
    cookies: dict[str, str],
    stop,
    hooks: StreamHooks,
    forced_disconnect_seconds: float = 180.0,
    random_value: Callable[[], float] = random.random,
) -> None:
    import requests

    failures = 0
    opened_once = False
    last_event_id = ""
    while not stop.is_set():
        if opened_once:
            hooks.reconnecting("sse", failures + 1, time.time())
        started = time.perf_counter()
        response = None
        connected = False
        try:
            headers = {"Accept": "text/event-stream"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            response = requests.get(
                f"{base_url}{api_prefix}/events/stream",
                headers=headers,
                cookies=cookies,
                stream=True,
                timeout=(5, 70),
            )
            response.raise_for_status()
            hooks.opened("sse", opened_once, (time.perf_counter() - started) * 1000)
            connected = True
            opened_once = True
            failures = 0
            connected_at = time.monotonic()
            for raw in response.iter_lines(decode_unicode=True):
                if stop.is_set():
                    break
                line = str(raw or "")
                if line.startswith("id:"):
                    last_event_id = line[3:].strip() or last_event_id
                if forced_disconnect_seconds > 0 and time.monotonic() - connected_at >= forced_disconnect_seconds:
                    break
        except Exception as exc:
            failures += 1
            hooks.failed("sse", type(exc).__name__)
        finally:
            if response is not None:
                response.close()
            if connected:
                hooks.closed("sse")
        if stop.is_set():
            return
        delay = 120.0 if failures >= 6 else retry_delay(1.0, max(1, failures), maximum_seconds=30, random_value=random_value)
        if _delay(stop, delay):
            return


def run_query_websocket(
    *,
    base_url: str,
    api_prefix: str,
    parser_type: str,
    cookies: dict[str, str],
    stop,
    hooks: StreamHooks,
    forced_disconnect_seconds: float = 180.0,
    random_value: Callable[[], float] = random.random,
) -> None:
    import websocket

    failures = 0
    opened_once = False
    last_event_id = 0
    data_version = ""
    target = websocket_url(base_url, api_prefix, parser_type)
    origin = urlparse(base_url).geturl().rstrip("/")
    while not stop.is_set():
        if opened_once:
            hooks.reconnecting("websocket", failures + 1, time.time())
        started = time.perf_counter()
        socket = None
        connected = False
        try:
            socket = websocket.create_connection(
                target,
                cookie=cookie_header(cookies),
                origin=origin,
                timeout=70,
                enable_multithread=False,
            )
            hooks.opened("websocket", opened_once, (time.perf_counter() - started) * 1000)
            connected = True
            if last_event_id:
                socket.send(json.dumps({
                    "type": "resume",
                    "after_event_id": last_event_id,
                    "data_version": data_version,
                }))
            opened_once = True
            failures = 0
            connected_at = time.monotonic()
            while not stop.is_set():
                raw = socket.recv()
                if not raw:
                    break
                payload = json.loads(raw)
                event_id = payload.get("event_id")
                if isinstance(event_id, int):
                    last_event_id = max(last_event_id, event_id)
                version = payload.get("data_version")
                if isinstance(version, str) and version:
                    data_version = version
                if forced_disconnect_seconds > 0 and time.monotonic() - connected_at >= forced_disconnect_seconds:
                    break
        except Exception as exc:
            failures += 1
            hooks.failed("websocket", type(exc).__name__)
        finally:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            if connected:
                hooks.closed("websocket")
        if stop.is_set():
            return
        delay = 120.0 if failures >= 6 else retry_delay(1.0, max(1, failures), maximum_seconds=30, random_value=random_value)
        if _delay(stop, delay):
            return
