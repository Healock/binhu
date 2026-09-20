"""Normalized MAC values shared by the residence lookup and port 23333."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any


DEFAULT_SERVER_MAC = "02:00:00:00:00:01"
SERVER_MAC_CONFIG_KEY = "server_mac_address"
_MAC_COMPACT_RE = re.compile(r"^[0-9A-F]{12}$")
_server_mac = DEFAULT_SERVER_MAC
_compat_server: asyncio.AbstractServer | None = None


def normalize_mac_address(value: Any) -> str:
    compact = re.sub(r"[.\-:\s]", "", str(value or "")).upper()
    if not _MAC_COMPACT_RE.fullmatch(compact):
        raise ValueError("MAC 地址必须是 12 位十六进制字符")
    normalized = ":".join(
        compact[index:index + 2] for index in range(0, 12, 2)
    )
    if normalized == "00:00:00:00:00:00" or int(compact[:2], 16) & 1:
        raise ValueError("MAC 地址必须是有效的单播地址")
    return normalized


def current_server_mac() -> str:
    return _server_mac


def set_server_mac(value: Any) -> str:
    global _server_mac
    _server_mac = normalize_mac_address(value)
    return _server_mac


async def load_server_mac(conn) -> str:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_value FROM _system_config WHERE config_key=%s",
            (SERVER_MAC_CONFIG_KEY,),
        )
        row = await cur.fetchone()
    if not row:
        return set_server_mac(DEFAULT_SERVER_MAC)
    return set_server_mac(row[0])


def _response(status: str, body: bytes, *, methods: str = "GET, OPTIONS") -> bytes:
    headers = [
        f"HTTP/1.1 {status}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Access-Control-Allow-Origin: *",
        f"Access-Control-Allow-Methods: {methods}",
        "Cache-Control: no-store",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii") + body


async def _handle_compat_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
        if len(raw) > 8192:
            writer.write(_response("431 Request Header Fields Too Large", b'{"error":"request_too_large"}'))
        else:
            first_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = first_line.split(" ")
            method = parts[0].upper() if len(parts) == 3 else ""
            path = parts[1].split("?", 1)[0] if len(parts) == 3 else ""
            if method == "OPTIONS" and path in {"/", "/health"}:
                writer.write(_response("204 No Content", b""))
            elif method == "GET" and path == "/":
                body = json.dumps(
                    {"mac": current_server_mac()}, separators=(",", ":")
                ).encode("ascii")
                writer.write(_response("200 OK", body))
            elif method == "GET" and path == "/health":
                writer.write(_response("200 OK", b'{"status":"ok"}'))
            elif path in {"/", "/health"}:
                writer.write(_response("405 Method Not Allowed", b'{"error":"method_not_allowed"}'))
            else:
                writer.write(_response("404 Not Found", b'{"error":"not_found"}'))
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        with suppress(Exception):
            writer.write(_response("400 Bad Request", b'{"error":"bad_request"}'))
            await writer.drain()
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def start_mac_compat_server(
    *, host: str = "0.0.0.0", port: int = 23333
) -> asyncio.AbstractServer:
    global _compat_server
    if _compat_server is not None:
        return _compat_server
    _compat_server = await asyncio.start_server(_handle_compat_client, host, port)
    return _compat_server


async def stop_mac_compat_server() -> None:
    global _compat_server
    if _compat_server is None:
        return
    _compat_server.close()
    await _compat_server.wait_closed()
    _compat_server = None
