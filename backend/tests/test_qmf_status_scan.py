import asyncio
import inspect
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.qmf_status import QmfLegacyStatus, STATUS_COMPLETED_MATCH
from services.qmf_status_scan import (
    SCAN_CONCURRENCY,
    create_status_scan_run,
    ensure_qmf_status_scan_schema,
    maybe_launch_scheduled_scan,
    run_status_scan,
    valid_schedule_time,
)
from routers.mobile_tasks import _qmf_status_by_rows
from routers.qmf_registration import router as qmf_router


class _Cursor:
    def __init__(self, rows=None):
        self.statements = []
        self.rows = rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, cursor):
        self._conn = _Connection(cursor)

    def acquire(self):
        return _AcquireContext(self._conn)


class QmfStatusScanTests(unittest.IsolatedAsyncioTestCase):
    def test_scan_start_and_read_routes_use_separate_permissions(self):
        routes = {
            route.path: {
                dependency.call.__name__
                for dependency in route.dependant.dependencies
            }
            for route in qmf_router.routes
            if "status-scans" in route.path
        }
        self.assertEqual(
            routes["/api/qmf-registration/status-scans"],
            {"require_qmf_registration_execute"},
        )
        self.assertEqual(
            routes["/api/qmf-registration/status-scans/latest"],
            {"require_online_raw_view"},
        )
        self.assertEqual(
            routes["/api/qmf-registration/status-scans/{run_id}"],
            {"require_online_raw_view"},
        )

    def test_schedule_time_validation_is_strict(self):
        self.assertTrue(valid_schedule_time("00:00"))
        self.assertTrue(valid_schedule_time("23:59"))
        for value in ("", "7:00", "24:00", "12:60", "2026-08-17 07:00"):
            with self.subTest(value=value):
                self.assertFalse(valid_schedule_time(value))

    def test_target_freeze_covers_all_completed_and_incremental_rules(self):
        source = inspect.getsource(create_status_scan_run)
        self.assertIn("projection.task_state='completed'", source)
        self.assertNotIn("projection.source_count=1", source)
        self.assertNotIn("projection.conflict=0", source)
        self.assertIn("snapshot.row_key IS NULL", source)
        self.assertIn("snapshot.error_code<>''", source)
        self.assertIn("snapshot.source_revision<>source.revision", source)
        self.assertIn("INTERVAL 7 DAY", source)

    async def test_schema_contains_only_safe_snapshot_fields(self):
        cursor = _Cursor()
        await ensure_qmf_status_scan_schema(cursor)
        sql = " ".join(statement for statement, _params in cursor.statements)
        self.assertIn("_qmf_status_scan_runs", sql)
        self.assertIn("_qmf_status_scan_items", sql)
        self.assertIn("_qmf_status_snapshots", sql)
        self.assertIn("identity_hmac", sql)
        for forbidden in (
            "identity_number", "phone", "address", "photo_base64", "station"
        ):
            self.assertNotIn(forbidden, sql)

    async def test_daily_scan_waits_for_configured_shanghai_time(self):
        cursor = _Cursor()
        config = SimpleNamespace(status_scan_enabled=True, status_scan_time="07:00")
        create = AsyncMock(return_value=(8, 3))
        with (
            patch("services.qmf_status_scan._pool", return_value=_Pool(cursor)),
            patch("services.qmf_status_scan.load_qmf_config", AsyncMock(return_value=config)),
            patch("services.qmf_status_scan.create_status_scan_run", create),
        ):
            before = await maybe_launch_scheduled_scan(datetime(
                2026, 8, 17, 6, 59, tzinfo=ZoneInfo("Asia/Shanghai")
            ))
            started = await maybe_launch_scheduled_scan(datetime(
                2026, 8, 17, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ))

        self.assertIsNone(before)
        self.assertEqual(started, 8)
        create.assert_awaited_once_with(
            trigger_source="scheduled",
            requested_by=None,
            scheduled_date=datetime(2026, 8, 17).date(),
        )

    async def test_scan_uses_one_session_and_never_exceeds_four_queries(self):
        cursor = _Cursor()
        items = [
            {
                "id": index,
                "parser_type": "疑似未注销模型三",
                "row_key": f"{index:032d}",
                "source_id": index,
                "expected_revision": 1,
                "expected_row_hash": "a" * 64,
                "identity_hmac": "b" * 64,
                "expected_result": "在吴",
            }
            for index in range(1, 13)
        ]
        claim_lock = asyncio.Lock()
        active = 0
        maximum_active = 0
        session_enters = 0

        async def claim(_run_id):
            async with claim_lock:
                return items.pop(0) if items else None

        async def current(item):
            return "11010519491231002X", item["expected_result"]

        class _Session:
            async def __aenter__(self):
                nonlocal session_enters
                session_enters += 1
                return self

            async def __aexit__(self, *_args):
                return None

            async def query(self, **_kwargs):
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1
                return QmfLegacyStatus(
                    state=STATUS_COMPLETED_MATCH,
                    result="在吴",
                )

        class _Client:
            def session(self):
                return _Session()

        finish_item = AsyncMock()
        finish_run = AsyncMock()
        with (
            patch("services.qmf_status_scan._pool", return_value=_Pool(cursor)),
            patch("services.qmf_status_scan._claim_item", side_effect=claim),
            patch("services.qmf_status_scan._current_item_context", side_effect=current),
            patch("services.qmf_status_scan._finish_item", finish_item),
            patch("services.qmf_status_scan._finish_run", finish_run),
            patch("services.qmf_status_scan.QmfLegacyStatusClient", return_value=_Client()),
        ):
            await run_status_scan(7)

        self.assertEqual(session_enters, 1)
        self.assertEqual(maximum_active, SCAN_CONCURRENCY)
        self.assertEqual(finish_item.await_count, 12)
        finish_run.assert_awaited_once_with(7, stopped_code="")

    async def test_task_snapshot_states_distinguish_not_scanned_stale_and_error(self):
        source_rows = [("row-a", json.dumps({"核查结果": "近期返吴"}))]
        not_scanned = await _qmf_status_by_rows(
            _Cursor([]),
            "疑似未注销模型三",
            source_rows,
        )
        self.assertEqual(not_scanned["row-a"]["state"], "not_scanned")

        snapshot = (
            "row-a", 10, 2, "a" * 64, "近期返吴",
            "completed_match", "近期返吴", "2026-08-17 07:00:00",
            "legacy_manual_or_other", "",
            datetime.utcnow(), 10, 2, "a" * 64,
        )
        matched = await _qmf_status_by_rows(
            _Cursor([snapshot]),
            "疑似未注销模型三",
            source_rows,
        )
        self.assertEqual(matched["row-a"]["state"], "completed_match")

        errored = list(snapshot)
        errored[9] = "station_mismatch"
        errored[12] = 3
        failed = await _qmf_status_by_rows(
            _Cursor([tuple(errored)]),
            "疑似未注销模型三",
            source_rows,
        )
        self.assertEqual(failed["row-a"]["state"], "error")


if __name__ == "__main__":
    unittest.main()
