import json
import os
from datetime import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import admin_ops


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = ""
        self.params = None
        self.limits = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.limits.append(int(params[-1]) if params else None)

    async def fetchall(self):
        limit = self.limits[-1]
        # aiomysql returns a tuple, including for an empty result.
        return tuple(self.rows[:limit] if limit is not None else self.rows)

    async def fetchone(self):
        return (self.rows[0][0] if self.rows else 0,)


class _Connection:
    def __init__(self, rows=None):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.released = 0

    async def acquire(self):
        return self.connection

    def release(self, connection):
        del connection
        self.released += 1


def _row(row_id=17):
    return (
        row_id, 3, "operator", "user.update", "user", "#3", "success",
        '{"password":"should-not-be-streamed","status":"active"}',
        "127.0.0.1", "secret-agent", datetime(2026, 9, 7, 1, 2, 3),
        "Operator", "Member", "operator-account",
    )


class AuditStreamTests(IsolatedAsyncioTestCase):
    async def test_query_uses_overlap_and_action_filter(self):
        connection = _Connection([_row()])
        pool = _Pool(connection)
        with patch.object(admin_ops.db_manager, "get_pool", return_value=pool):
            rows = await admin_ops._read_audit_rows_after(20_000, "user.update")
        self.assertEqual(rows, [_row()])
        self.assertIn("audit.id > %s", connection.cursor_obj.sql)
        self.assertIn("audit.action=%s", connection.cursor_obj.sql)
        self.assertEqual(connection.cursor_obj.params[0], 10_000)
        self.assertEqual(connection.cursor_obj.params[1], 20_000)
        self.assertEqual(connection.cursor_obj.params[2], "user.update")
        self.assertEqual(connection.cursor_obj.params[3], admin_ops.AUDIT_STREAM_CURSOR_OVERLAP)
        self.assertEqual(pool.released, 1)

    async def test_query_without_action_keeps_forward_and_overlap_parameters(self):
        connection = _Connection([_row()])
        pool = _Pool(connection)
        with patch.object(admin_ops.db_manager, "get_pool", return_value=pool):
            await admin_ops._read_audit_rows_after(20_000)
        self.assertEqual(connection.cursor_obj.params, (10_000, 20_000, admin_ops.AUDIT_STREAM_CURSOR_OVERLAP))

    async def test_overlap_reads_more_than_forward_page(self):
        rows = [_row(index) for index in range(1, 251)]
        connection = _Connection(rows)
        pool = _Pool(connection)
        with patch.object(admin_ops.db_manager, "get_pool", return_value=pool):
            result = await admin_ops._read_audit_rows_after(300)
        self.assertEqual(len(result), 250)
        self.assertEqual(connection.cursor_obj.params, (0, 300, admin_ops.AUDIT_STREAM_CURSOR_OVERLAP))
        self.assertEqual(connection.cursor_obj.limits, [admin_ops.AUDIT_STREAM_PAGE_SIZE, admin_ops.AUDIT_STREAM_CURSOR_OVERLAP])

    def test_stream_payload_excludes_private_audit_fields(self):
        payload = admin_ops._serialize_audit_row(_row(), stream=True)
        self.assertEqual(payload["id"], 17)
        self.assertIsNone(payload["detail"])
        self.assertEqual(payload["detail_items"], [])
        self.assertNotIn("ip_address", payload)
        self.assertNotIn("user_agent", payload)
        self.assertNotIn("password", json.dumps(payload))
        self.assertNotIn("secret-agent", json.dumps(payload))

    async def test_generator_emits_sse_id_and_safe_payload(self):
        class Request:
            async def is_disconnected(self):
                return False

            headers = {}

        stop = admin_ops.asyncio.Event()
        with (
            patch.object(admin_ops, "_read_audit_rows_after", new=AsyncMock(return_value=[_row()])),
            patch.object(admin_ops, "get_current_user", new=AsyncMock(return_value={"id": 3})),
            patch.object(admin_ops, "require_super_admin", new=AsyncMock(return_value={"id": 3})),
        ):
            generator = admin_ops._audit_event_generator(
                Request(), {"id": 3}, action="", cursor=0, stop_event=stop,
            )
            first = await anext(generator)
        self.assertIn("id: 17\n", first)
        self.assertIn("event: audit_record\n", first)
        self.assertNotIn("secret-agent", first)
        await generator.aclose()

    async def test_generator_uses_monotonic_max_cursor_for_delayed_rows(self):
        class Request:
            async def is_disconnected(self):
                return False

            headers = {}

        stop = admin_ops.asyncio.Event()
        with (
            patch.object(admin_ops, "_read_audit_rows_after", new=AsyncMock(return_value=[_row(8), _row(120)])),
            patch.object(admin_ops, "get_current_user", new=AsyncMock(return_value={"id": 3})),
            patch.object(admin_ops, "require_super_admin", new=AsyncMock(return_value={"id": 3})),
        ):
            generator = admin_ops._audit_event_generator(
                Request(), {"id": 3}, action="", cursor=100, stop_event=stop,
            )
            first = await anext(generator)
            second = await anext(generator)
        self.assertIn("id: 100\n", first)
        self.assertIn("id: 120\n", second)
        await generator.aclose()

    async def test_generator_announces_deliberate_connection_replacement(self):
        class Request:
            async def is_disconnected(self):
                return False

            headers = {}

        stop = admin_ops.asyncio.Event()
        with (
            patch.object(admin_ops, "_read_audit_rows_after", new=AsyncMock(return_value=[])),
            patch.object(admin_ops, "get_current_user", new=AsyncMock(return_value={"id": 3})),
            patch.object(admin_ops, "require_super_admin", new=AsyncMock(return_value={"id": 3})),
        ):
            generator = admin_ops._audit_event_generator(
                Request(), {"id": 3}, action="", cursor=0, stop_event=stop,
            )
            keep_alive = await anext(generator)
            self.assertIn(": keep-alive", keep_alive)
            stop.set()
            replaced = await anext(generator)
        self.assertEqual(replaced, "event: replaced\ndata: {}\n\n")
        await generator.aclose()

    async def test_generator_applies_action_filter_without_advancing_past_unmatched_rows(self):
        class Request:
            async def is_disconnected(self):
                return False

            headers = {}

        stop = admin_ops.asyncio.Event()
        rows = [_row(18), (*_row(19)[:3], "wanted", *_row(19)[4:])]
        with (
            patch.object(admin_ops, "_read_audit_rows_after", new=AsyncMock(return_value=rows)),
            patch.object(admin_ops, "get_current_user", new=AsyncMock(return_value={"id": 3})),
            patch.object(admin_ops, "require_super_admin", new=AsyncMock(return_value={"id": 3})),
        ):
            generator = admin_ops._audit_event_generator(
                Request(), {"id": 3}, action="wanted", cursor=0, stop_event=stop,
            )
            first = await anext(generator)
        self.assertIn("id: 19\n", first)
        self.assertNotIn("id: 18\n", first)
        await generator.aclose()

    async def test_stream_route_prefers_last_event_id_over_query_cursor(self):
        class Request:
            headers = {"last-event-id": "42"}

        captured = {}

        async def fake_generator(request, user, *, action, cursor, stop_event):
            del request, user, action, stop_event
            captured["cursor"] = cursor
            yield ": keep-alive\n\n"

        with patch.object(admin_ops, "_audit_event_generator", new=fake_generator):
            response = await admin_ops.stream_audit_records(
                Request(), action="user.update", after_id=7, user={"id": 3},
            )
            body = [item async for item in response.body_iterator]
        self.assertEqual(captured["cursor"], 42)
        self.assertEqual(body, [": keep-alive\n\n"])
