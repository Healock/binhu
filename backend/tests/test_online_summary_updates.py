from datetime import date
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import unittest
import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.online_summary_updates import (
    ensure_online_summary_update_schema,
    enqueue_online_summary_update,
    effective_workload_transition,
    effective_workload_for_update,
    should_apply_revision,
)
import services.online_summary_updates as summary_updates
from services.report_builders import summary as report_summary


class FakeCursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 0

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        self.rowcount = 1

    async def executemany(self, statement, params):
        self.statements.append((statement, list(params)))
        self.rowcount = len(params)

    async def fetchone(self):
        return None


class _SummaryCursor:
    def __init__(self, *, one_rows, many_rows):
        self.one_rows = list(one_rows)
        self.many_rows = list(many_rows)
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))

    async def fetchone(self):
        return self.one_rows.pop(0)

    async def fetchall(self):
        return self.many_rows.pop(0)


def _summary_pool(cursor):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=cursor)
    context.__aexit__ = AsyncMock(return_value=None)
    connection = MagicMock()
    connection.cursor.return_value = context
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=connection)
    pool.release = MagicMock()
    return pool


class SummaryContractTests(unittest.IsolatedAsyncioTestCase):
    def trigger(self):
        return dict(id=1, task_id=42, parser_type="疑似返苏", row_key="a" * 32,
                    revision=5, business_date=date(2026, 9, 7), attempt_count=1)

    async def test_superseded_trigger_never_opens_daily_transaction(self):
        row = self.trigger()
        with patch.object(summary_updates, "_load_authoritative_task", new=AsyncMock(
            return_value=dict(task_id=42, revision=6)
        )), patch.object(summary_updates, "_daily_pool") as pool:
            with self.assertRaises(summary_updates.StaleSummaryTrigger):
                await summary_updates._apply_update(row)
            pool.assert_not_called()

    async def test_equal_or_newer_ledger_revision_is_noop(self):
        for revision in (5, 6):
            with self.subTest(revision=revision):
                cursor = _SummaryCursor(one_rows=[("completed", 1, revision)], many_rows=[])
                pool = _summary_pool(cursor)
                conn = pool.acquire.return_value
                conn.begin = AsyncMock()
                conn.commit = AsyncMock()
                conn.rollback = AsyncMock()
                with patch.object(summary_updates, "_load_authoritative_task", new=AsyncMock(
                    return_value=dict(task_id=42, revision=5)
                )), patch.object(summary_updates, "_ensure_incremental_report_tables", new=AsyncMock()), \
                    patch.object(summary_updates, "_daily_pool", return_value=pool):
                    await summary_updates._apply_update(self.trigger())
                self.assertEqual(len(cursor.statements), 1)
                self.assertIn("FOR UPDATE", cursor.statements[0][0])
                conn.commit.assert_awaited_once()
                pool.release.assert_called_once_with(conn)

    async def test_revision_changes_during_aggregation_roll_back_ledger(self):
        current = dict(task_id=42, revision=5, community="虚构社区", inspector="测试员",
                       task_state="completed", unable_to_verify=0, reached_bottom=1)
        cursor = _SummaryCursor(one_rows=[("unchecked", 0, 4, "虚构社区", "测试员", 1, 0, 0), (0,)], many_rows=[])
        pool = _summary_pool(cursor)
        conn = pool.acquire.return_value
        conn.begin = AsyncMock()
        conn.commit = AsyncMock()
        conn.rollback = AsyncMock()
        with patch.object(summary_updates, "_load_authoritative_task", new=AsyncMock(
            side_effect=[current, current, {**current, "revision": 6}]
        )), patch.object(summary_updates, "_ensure_incremental_report_tables", new=AsyncMock()), \
            patch.object(summary_updates, "_daily_pool", return_value=pool), \
            patch.object(summary_updates, "_refresh_affected_report_groups", new=AsyncMock()) as refresh:
            with self.assertRaises(summary_updates.StaleSummaryTrigger):
                await summary_updates._apply_update(self.trigger())
        refresh.assert_awaited_once()
        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()
        pool.release.assert_called_once_with(conn)

    async def test_worker_failure_retries_then_stops_at_limit(self):
        with patch.object(summary_updates, "_apply_update", new=AsyncMock(side_effect=RuntimeError("private"))), \
            patch.object(summary_updates, "_finish_update", new=AsyncMock()) as finish:
            await summary_updates._process_update(self.trigger())
            finish.assert_awaited_with(1, "retry", "summary_update_failed")
            await summary_updates._process_update({**self.trigger(), "attempt_count": 5})
            finish.assert_awaited_with(1, "failed", "summary_update_failed")

    async def test_summary_update_schema_has_task_revision_date_idempotency_key(self):
        cursor = FakeCursor()

        await ensure_online_summary_update_schema(cursor)

        schema = cursor.statements[0][0]
        assert "_online_summary_updates" in schema
        assert "task_id" in schema
        assert "revision" in schema
        assert "business_date" in schema
        assert "finished_at" in schema
        assert "UNIQUE KEY uk_online_summary_update" in schema
        assert "(parser_type, task_id, revision, business_date)" in schema

    async def test_enqueue_summary_update_stores_metadata_only_and_is_idempotent(self):
        cursor = FakeCursor()

        inserted = await enqueue_online_summary_update(
            cursor,
            task_id=42,
            parser_type="疑似返苏",
            row_key="a" * 32,
            revision=7,
            business_date=date(2026, 9, 7),
            operation_id="op-1",
        )

        assert inserted is True
        statement, params = cursor.statements[0]
        assert "INSERT INTO _online_summary_updates" in statement
        assert "ON DUPLICATE KEY UPDATE" in statement
        assert params == (42, "疑似返苏", "a" * 32, 7, date(2026, 9, 7), "op-1")
        assert "values_json" not in statement
        assert "身份证" not in statement
        assert "手机号" not in statement

    def test_summary_update_rejects_stale_or_duplicate_revision(self):
        assert should_apply_revision(None, 1) is True
        assert should_apply_revision(1, 1) is False
        assert should_apply_revision(3, 2) is False
        assert should_apply_revision(3, 4) is True

    def test_effective_workload_transition_is_idempotent_for_same_state(self):
        for previous, current, expected in [(None, 'unchecked', 0), (None, 'checked', 1), ('unchecked', 'checked', 1), ('unchecked', 'completed', 1), ('checked', 'completed', 1), ('completed', 'completed', 0), ('checked', 'checked', 0)]:
            assert effective_workload_transition(previous, current) == expected

    def test_effective_workload_for_update_is_one_per_day_and_two_day_capped(self):
        for previous, historical, transition, expected in [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 0), (0, 0, 0, 0)]:
            assert effective_workload_for_update(previous, historical, transition) == expected

    def test_incremental_report_table_ddl_is_outside_refresh_transaction(self):
        helper_source = inspect.getsource(summary_updates._ensure_incremental_report_tables)
        refresh_source = inspect.getsource(summary_updates._refresh_affected_report_groups)

        assert "CREATE TABLE IF NOT EXISTS _daily_report_meta" in helper_source
        assert "CREATE TABLE IF NOT EXISTS" in helper_source
        assert "_daily_report_meta" in helper_source
        assert "CREATE TABLE" not in refresh_source
        assert "_daily_report_meta" not in refresh_source
        assert "SHOW TABLES" not in refresh_source

    async def test_public_summary_reads_worker_materialization_without_summary_marker(self):
        inspector_row = ("社区甲", "张三", 1, 0, 0, 1, 1.0, 0, 1.0)
        cursor = _SummaryCursor(
            one_rows=[
                ("2026-09-07_daily_suspectReturn_inspector", "incremental"),
                ("2026-09-07_daily_suspectReturn_inspector",),
            ],
            many_rows=[[inspector_row]],
        )
        pool = _summary_pool(cursor)
        attendance = {
            "complete": True,
            "missing_week_starts": [],
            "history_started_on": "2026-07-01",
            "legacy_history_incomplete": False,
        }

        with patch.object(report_summary.db_manager, "get_pool", return_value=pool), \
            patch.object(report_summary, "_get_summary_types", new=AsyncMock(return_value=["疑似返苏"])), \
            patch.object(report_summary, "get_community_alias_lookup", new=AsyncMock(return_value={"社区甲": "社区甲"})), \
            patch.object(report_summary, "complete_inspector_rows", new=AsyncMock(return_value=[inspector_row])), \
            patch.object(report_summary, "get_active_members", new=AsyncMock(return_value=[("社区甲", "张三")])), \
            patch.object(report_summary, "load_community_person_days", new=AsyncMock(return_value=({"社区甲": 1}, attendance))), \
            patch.object(report_summary, "load_effective_workload_by_community", new=AsyncMock(return_value={"社区甲": 1})):
            result = await report_summary.get_summary("2026-09-07")

        assert result["exists"] is True
        assert result["community"]["data"][0]["数据总数"] == 1
        source_sql = " ".join(cursor.statements[0][0].split())
        assert "SELECT table_name, generation_method FROM _daily_report_meta" in source_sql
        assert not any("daily_summary" in statement for statement, _ in cursor.statements)
