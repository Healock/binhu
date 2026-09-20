import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import mac_address as mac_router  # noqa: E402
from services.mac_address import (  # noqa: E402
    DEFAULT_SERVER_MAC,
    current_server_mac,
    load_server_mac,
    normalize_mac_address,
    set_server_mac,
    start_mac_compat_server,
    stop_mac_compat_server,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, query, params=None):
        self.connection.queries.append((str(query), params))

    async def fetchone(self):
        return self.connection.row


class FakeConnection:
    def __init__(self, row=None):
        self.row = row
        self.queries = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    async def commit(self):
        self.commits += 1


async def request_compat(port: int, method: str, path: str = "/") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("ascii")
    )
    await writer.drain()
    payload = await reader.read()
    writer.close()
    await writer.wait_closed()
    status = int(payload.split(b" ", 2)[1])
    return status, payload.split(b"\r\n\r\n", 1)[1]


class MacAddressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await stop_mac_compat_server()
        set_server_mac(DEFAULT_SERVER_MAC)

    def test_normalizes_only_valid_unicast_addresses(self):
        self.assertEqual(normalize_mac_address("02-11-22-33-44-66"), "02:11:22:33:44:66")
        self.assertEqual(normalize_mac_address("0211.2233.4466"), "02:11:22:33:44:66")
        for value in ("not-a-mac", "00:00:00:00:00:00", "01:11:22:33:44:66"):
            with self.assertRaises(ValueError):
                normalize_mac_address(value)

    def test_api_payload_is_normalized_during_validation(self):
        self.assertEqual(mac_router.ServerMacUpdate(mac="02-11-22-33-44-66").mac, "02:11:22:33:44:66")
        with self.assertRaises(ValidationError):
            mac_router.ServerMacUpdate(mac="01:11:22:33:44:66")

    async def test_invalid_stored_mac_fails_closed(self):
        with self.assertRaises(ValueError):
            await load_server_mac(FakeConnection(("invalid",)))

    async def test_compat_server_is_read_only_and_reflects_updates(self):
        server = await start_mac_compat_server(host="127.0.0.1", port=0)
        port = int(server.sockets[0].getsockname()[1])
        set_server_mac("02:11:22:33:44:66")
        status, body = await request_compat(port, "GET")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"mac":"02:11:22:33:44:66"}')
        self.assertEqual((await request_compat(port, "GET", "/health"))[0], 200)
        self.assertEqual((await request_compat(port, "OPTIONS"))[0], 204)
        self.assertEqual((await request_compat(port, "POST"))[0], 405)

    async def test_update_persists_mac_clears_sessions_and_redacts_audit(self):
        connection = FakeConnection()
        request = Request({
            "type": "http",
            "method": "PUT",
            "path": "/api/system/server-mac",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("test", 80),
        })
        audit = AsyncMock()
        with patch.object(mac_router, "record_admin_audit", audit), patch.object(
            mac_router, "wake_residence_lookup_scheduler"
        ) as wake:
            result = await mac_router.update_server_mac(
                mac_router.ServerMacUpdate(mac="02-11-22-33-44-66"),
                request,
                {"id": 7, "role": "admin"},
                connection,
            )

        self.assertEqual(result["mac"], "02:11:22:33:44:66")
        self.assertEqual(current_server_mac(), "02:11:22:33:44:66")
        self.assertEqual(connection.commits, 2)
        self.assertIn("INSERT INTO _system_config", connection.queries[0][0])
        self.assertIn("DELETE FROM _system_config", connection.queries[1][0])
        self.assertEqual(audit.await_args.kwargs["detail"], {"key": "server_mac_address"})
        self.assertIs(audit.await_args.kwargs["conn"], connection)
        self.assertNotIn("02:11:22:33:44:66", str(audit.await_args))
        wake.assert_called_once_with(force_full_scan=True)


if __name__ == "__main__":
    unittest.main()
