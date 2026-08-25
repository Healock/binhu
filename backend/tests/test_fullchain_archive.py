import io
import json
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException
from openpyxl import Workbook, load_workbook

from routers.fullchain_archive import (
    _candidate_rows,
    _parse_deadline,
    _preview_token,
    require_fullchain_archive,
)
from routers.police_dispatch import require_police_access
from services.permissions import POLICE_DISPATCH_MANAGE
from services.fullchain_archive import build_archive_workbook, parse_police_raw
from services.fullchain_archive_jobs import (
    _acquire_sheet_lock_with_retry,
    _safe_error_code,
    _stage_platform_archive,
)
from services.report_builders.base import BaseReportBuilder
from services.task_workflow import TASK_WORKFLOWS


def workbook_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class FullchainArchiveTests(unittest.TestCase):
    @patch("services.fullchain_archive.hmac_digest", side_effect=lambda value, kind: (f"digest-{value}", 1))
    def test_police_raw_parser_finds_identity_column_and_deduplicates(self, _digest):
        content = workbook_bytes([
            ["说明"],
            ["姓名", "身份证号码", "反馈结果"],
            ["甲", "320000199001010011", "在册"],
            ["乙", "320000199001010011", "在册"],
            ["丙", "320000199001010022", ""],
        ])
        result = parse_police_raw(content, "公安网.xlsx")
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["identity_hmacs"], [
            "digest-320000199001010011", "digest-320000199001010022",
        ])
        self.assertNotIn("320000199001010011", str(result["preview"]))

    def test_archive_workbook_keeps_sensitive_numbers_as_text(self):
        content = build_archive_workbook([{
            "physical_row": 20, "source": "测试", "name": "甲",
            "identity": "320000199001010010", "phone": "13800000000",
            "address": "测试地址", "registration": "", "result": "离苏",
            "category": "离苏",
        }], 12, __import__("datetime").datetime(2026, 8, 21, 10, 0))
        workbook = load_workbook(io.BytesIO(content), data_only=False)
        sheet = workbook["离苏"]
        self.assertEqual(sheet.cell(2, 4).value, "320000199001010010")
        self.assertEqual(sheet.cell(2, 4).data_type, "s")
        self.assertEqual(sheet.cell(2, 5).value, "13800000000")
        workbook.close()

    def test_deadline_parsing_uses_business_year_and_handles_full_date(self):
        today = date(2026, 8, 21)
        self.assertEqual(_parse_deadline("08-20", today), date(2026, 8, 20))
        self.assertEqual(_parse_deadline("2026-08-22", today), date(2026, 8, 22))
        self.assertIsNone(_parse_deadline("未知", today))

    def test_preview_token_changes_with_source_revision(self):
        item = {"source_id": 1, "revision": 1, "row_hash": "a" * 64, "category": "离苏"}
        changed = dict(item, revision=2)
        self.assertNotEqual(_preview_token([item]), _preview_token([changed]))

    def test_transfer_options_are_split_but_legacy_value_remains_readable(self):
        options = TASK_WORKFLOWS["全链条"].result_options
        self.assertIn("移交（所内）", options)
        self.assertIn("移交（所外）", options)
        self.assertIn("移交", options)

    def test_report_category_prefers_specific_transfer_value(self):
        builder = BaseReportBuilder()
        sql = builder.ledger_result_category_sql("snapshot")
        self.assertLess(sql.index("移交（所内）"), sql.index("LIKE '%%移交%%'"))

    def test_background_error_codes_never_persist_raw_exception_text(self):
        self.assertEqual(
            _safe_error_code(RuntimeError("source_row_changed"), "delete_failed"),
            "source_row_changed",
        )
        self.assertEqual(
            _safe_error_code(RuntimeError("upstream body with personal data"), "delete_failed"),
            "delete_failed",
        )


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executions = []

    async def execute(self, sql, params=()):
        self.executions.append((sql, params))

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FullchainArchiveAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_lock_retries_until_previous_write_releases(self):
        attempts = []

        async def acquire(_cur, spreadsheet_id, timeout):
            attempts.append((spreadsheet_id, timeout))
            return len(attempts) == 3

        sleep = AsyncMock()
        self.assertTrue(await _acquire_sheet_lock_with_retry(
            object(), 7, acquire=acquire, sleep=sleep, retry_delays=(1, 2, 3),
        ))
        self.assertEqual(len(attempts), 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
        self.assertTrue(all(timeout == 5 for _, timeout in attempts))

    async def test_archive_lock_retry_has_a_bounded_wait(self):
        acquire = AsyncMock(return_value=False)
        sleep = AsyncMock()
        self.assertFalse(await _acquire_sheet_lock_with_retry(
            object(), 7, acquire=acquire, sleep=sleep, retry_delays=(1, 2),
        ))
        self.assertEqual(acquire.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])

    @patch("routers.fullchain_archive._latest_raw_upload", new=AsyncMock(return_value=None))
    @patch("routers.fullchain_archive.get_business_date", new=AsyncMock(return_value=date(2026, 8, 21)))
    async def test_duplicate_projection_is_visible_but_cannot_archive(self):
        values = {
            "姓名": "测试人员",
            "身份证号": "320000199001010011",
            "核查结果": "离苏",
        }
        cursor = _Cursor([(
            7, "a" * 32, 3, "b" * 64, 20, 4, "sheet-1",
            json.dumps(values, ensure_ascii=False), "c" * 64, 2, 1, "", "",
        )])

        rows = await _candidate_rows(cursor)

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["eligible"])
        self.assertIn("重复或冲突来源行", rows[0]["reason"])

    @patch(
        "services.fullchain_archive_jobs.get_database_column_map",
        new=AsyncMock(return_value={"姓名": "姓名"}),
    )
    async def test_platform_archive_is_idempotent_per_export(self):
        cursor = _Cursor()
        parser = type("Parser", (), {"table_name": "t_fullchain", "COLUMNS": ["姓名"]})()

        await _stage_platform_archive(
            _Connection(cursor), parser, 123, "a" * 32, {"姓名": "测试人员"}
        )

        self.assertEqual(len(cursor.executions), 3)
        self.assertIn("DELETE FROM OnlineDataArchive.t_fullchain_archive", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][1], "fullchain_feedback_export:123")
        self.assertIn("_archive_reason", cursor.executions[1][0])
        self.assertIn("DELETE FROM `t_fullchain`", cursor.executions[2][0])

    async def test_archive_permission_requires_allowed_position_and_all_scope(self):
        allowed = {
            "permission_scopes": {"police.dispatch.manage": "all"},
            "data_scope": "all",
            "permission_groups": [],
            "role": "user",
            "member": {"position": "所队领导"},
        }
        self.assertIs(await require_fullchain_archive(user=allowed), allowed)

        with self.assertRaises(HTTPException) as denied_scope:
            await require_fullchain_archive(user={
                **allowed,
                "permission_scopes": {"police.dispatch.manage": "own_department"},
            })
        self.assertEqual(denied_scope.exception.status_code, 403)

        with self.assertRaises(HTTPException) as denied_position:
            await require_fullchain_archive(user={
                **allowed,
                "member": {"position": "组员"},
            })
        self.assertEqual(denied_position.exception.status_code, 403)

    async def test_archive_permission_does_not_expand_dispatch_workbench_positions(self):
        user = {
            "permission_scopes": {POLICE_DISPATCH_MANAGE: "all"},
            "data_scope": "all",
            "permission_groups": [],
            "role": "user",
            "member": {"position": "所队领导"},
        }
        self.assertIs(await require_police_access(POLICE_DISPATCH_MANAGE)(user=user), user)


if __name__ == "__main__":
    unittest.main()
