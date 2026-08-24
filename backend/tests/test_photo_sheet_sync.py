import asyncio
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, Mock, call, patch

import services.photo_sheet_sync as photo_sheet_sync

from services.photo_sheet_sync import (
    COLUMNS,
    ExistingPhotoSheetRow,
    SourceRowsCache,
    _pause_exhausted_outbox,
    _pair_relocated_rows,
    _pair_revised_rows,
    _canonical_revised_mapping,
    normalize_import_identity,
    historical_result,
    _locate_mapping,
    _process_append,
    _ticket_import_state,
    parse_rows,
    parse_source_url,
    preview_summary,
    outbox_retry_plan,
)


class AppendCursor:
    def __init__(self, ticket_details: dict[int, tuple]):
        self.ticket_details = ticket_details
        self.mappings: list[dict] = []
        self._one = None
        self._many = []

    async def execute(self, query, params=()):
        if query.startswith("SELECT physical_row FROM photo_sheet_rows WHERE work_order_id"):
            ticket_id = int(params[0])
            mapping = next((item for item in self.mappings if item["ticket_id"] == ticket_id), None)
            self._one = (mapping["physical_row"],) if mapping else None
            return
        if query.startswith("SELECT detail.community_name"):
            self._one = self.ticket_details[int(params[0])]
            return
        if query.startswith("SELECT physical_row FROM photo_sheet_rows WHERE source_id"):
            source_id = int(params[0])
            self._many = [
                (item["physical_row"],)
                for item in self.mappings
                if item["source_id"] == source_id and item["physical_row"] is not None
            ]
            return
        if query.startswith("INSERT INTO photo_sheet_rows"):
            self.mappings.append({
                "source_id": int(params[0]),
                "ticket_id": int(params[1]),
                "physical_row": int(params[2]),
            })
            return
        raise AssertionError(f"unexpected query: {query}")

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._many


def source_row(number: int, values: list[str], *, cell_meta: dict | None = None) -> dict:
    result = {
        "physical_row": number,
        "values": dict(zip(COLUMNS, values)),
    }
    if cell_meta is not None:
        result["cell_meta"] = cell_meta
    return result


class PhotoSheetParserTests(unittest.TestCase):
    def test_source_url_requires_file_and_tab(self):
        self.assertEqual(
            parse_source_url("https://docs.qq.com/sheet/DFictionalPhotoSheet?tab=FAKE01"),
            ("DFictionalPhotoSheet", "FAKE01"),
        )
        with self.assertRaises(ValueError):
            parse_source_url("https://docs.qq.com/sheet/DFictionalPhotoSheet")

    def test_identity_normalization_is_conservative(self):
        self.assertEqual(
            normalize_import_identity(" 32050020000101001x， "),
            ("32050020000101001X", ""),
        )
        self.assertEqual(normalize_import_identity(""), ("", "身份证号为空"))
        self.assertEqual(
            normalize_import_identity("32050020000101"),
            ("32050020000101", "身份证号格式异常"),
        )

    def test_marker_is_not_created_as_request_and_completes_previous_rows(self):
        rows = parse_rows([
            source_row(2, ["冬梅社区", "平安码", "甲", "32050020000101001X", "申请人甲", "2026/8/9", ""]),
            source_row(3, ["冬梅社区", "平安码", "乙", "320500200001010028", "申请人乙", "2026/8/10", "无照片"]),
            source_row(4, ["8.10-19.18", "", "", "", "", "", ""]),
            source_row(5, ["蠡湖社区", "模型三", "丙", "320500200001010036", "申请人丙", "2026/8/11", ""]),
        ])
        summary = preview_summary(rows)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["markers"], 1)
        self.assertEqual(summary["historical_completed"], 2)
        self.assertEqual(summary["pending_after_last_marker"], 1)
        marker = next(row for row in rows if row.kind == "marker")
        self.assertEqual(marker.marker_at, datetime(2026, 8, 10, 19, 18))
        self.assertTrue(marker.time_inferred)

    def test_full_width_marker_and_cross_year_inference(self):
        rows = parse_rows([
            source_row(2, ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/12/31", ""]),
            source_row(3, ["截止 1．1－08：30", "", "", "", "", "", ""]),
        ])
        marker = rows[1]
        self.assertEqual(marker.marker_at, datetime(2027, 1, 1, 8, 30))
        self.assertTrue(marker.time_inferred)

    def test_duplicate_rows_remain_separate_requests(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        rows = parse_rows([source_row(2, values), source_row(3, values)])
        summary = preview_summary(rows)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["duplicate_groups"], 1)
        self.assertNotEqual(rows[0].row_hash, rows[1].row_hash)
        self.assertEqual(rows[0].fingerprint, rows[1].fingerprint)

    def test_numeric_excel_serial_date_is_converted_only_for_number_cells(self):
        numeric = parse_rows([source_row(
            2,
            ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "46244", ""],
            cell_meta={"申请日期": {"type": "number"}},
        )])[0]
        text_value = parse_rows([source_row(
            3,
            ["冬梅社区", "来源", "乙", "320500200001010028", "申请人", "46244", ""],
            cell_meta={"申请日期": {"type": "text"}},
        )])[0]

        self.assertEqual(numeric.requested_at, datetime(2026, 8, 10))
        self.assertTrue(numeric.excel_date_converted)
        self.assertEqual(numeric.request_date_issue, "")
        self.assertIsNone(text_value.requested_at)
        self.assertFalse(text_value.excel_date_converted)
        self.assertEqual(text_value.request_date_issue, "invalid")

    def test_decimal_and_out_of_range_numbers_are_not_guessed_as_dates(self):
        rows = parse_rows([
            source_row(
                2,
                ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "8.1", ""],
                cell_meta={"申请日期": {"type": "number"}},
            ),
            source_row(
                3,
                ["冬梅社区", "来源", "乙", "320500200001010028", "申请人", "20260810", ""],
                cell_meta={"申请日期": {"type": "number"}},
            ),
        ])

        self.assertTrue(all(row.requested_at is None for row in rows))
        self.assertTrue(all(row.request_date_issue == "invalid" for row in rows))

    def test_text_date_with_time_keeps_calendar_date(self):
        row = parse_rows([source_row(
            2,
            ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026-08-10 16:30", ""],
            cell_meta={"申请日期": {"type": "text"}},
        )])[0]

        self.assertEqual(row.requested_at, datetime(2026, 8, 10))
        self.assertEqual(row.request_date_issue, "")

    def test_preview_token_depends_on_source_values_not_cell_type(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "46244", ""]
        numeric_summary = preview_summary(parse_rows([
            source_row(2, values, cell_meta={"申请日期": {"type": "number"}}),
        ]))
        text_summary = preview_summary(parse_rows([
            source_row(2, values, cell_meta={"申请日期": {"type": "text"}}),
        ]))

        self.assertEqual(numeric_summary["preview_token"], text_summary["preview_token"])
        self.assertEqual(numeric_summary["excel_date_converted_count"], 1)
        self.assertEqual(text_summary["request_date_invalid_count"], 1)

    def test_preview_separates_blocking_warnings_and_converted_dates(self):
        rows = parse_rows([
            source_row(
                2,
                ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "46244", ""],
                cell_meta={"申请日期": {"type": "number"}},
            ),
            source_row(3, ["冬梅社区", "来源", "乙", "", "申请人", "", ""]),
            source_row(4, ["无法识别-批次", "", "", "", "", "", ""]),
            source_row(5, ["蠡湖社区", "来源", "丙", "320500200001010036", "申请人", "", ""]),
        ])

        summary = preview_summary(rows)

        self.assertEqual(summary["blocking_issue_count"], 1)
        self.assertEqual(summary["warning_count"], 3)
        self.assertEqual(summary["identity_empty_count"], 1)
        self.assertEqual(summary["identity_invalid_count"], 0)
        self.assertEqual(summary["excel_date_converted_count"], 1)
        self.assertEqual(summary["request_date_missing_count"], 2)
        self.assertEqual(summary["request_date_invalid_count"], 0)
        self.assertEqual(summary["marker_time_invalid_count"], 1)
        self.assertEqual(summary["pending_blocking_count"], 0)
        self.assertEqual(summary["pending_warning_count"], 1)

    def test_pending_valid_identity_uses_observed_time_for_invalid_date(self):
        row = parse_rows([source_row(
            2, ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "", ""],
        )])[0]
        observed_at = datetime(2026, 8, 11, 10, 30)

        status, requested_at, issue = _ticket_import_state(
            row, completed=False, observed_at=observed_at,
        )

        self.assertEqual(status, "queued")
        self.assertEqual(requested_at, observed_at)
        self.assertIn("申请日期为空", issue)
        self.assertIn("申请日期由平台首次发现时间代替", issue)

    def test_pending_invalid_identity_still_waits_for_requester(self):
        row = parse_rows([source_row(
            2, ["冬梅社区", "来源", "甲", "", "申请人", "", ""],
        )])[0]

        status, requested_at, issue = _ticket_import_state(
            row, completed=False, observed_at=datetime(2026, 8, 11, 10, 30),
        )

        self.assertEqual(status, "pending_requester")
        self.assertIsNone(requested_at)
        self.assertIn("身份证号为空", issue)
        self.assertNotIn("首次发现时间代替", issue)

    def test_completed_history_does_not_invent_missing_request_date(self):
        row = parse_rows([source_row(
            2, ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "", ""],
        )])[0]

        status, requested_at, issue = _ticket_import_state(
            row, completed=True, observed_at=datetime(2026, 8, 11, 10, 30),
        )

        self.assertEqual(status, "completed")
        self.assertIsNone(requested_at)
        self.assertEqual(issue, "申请日期为空")

    def test_historical_g_failure_note_only_changes_result_classification(self):
        self.assertEqual(historical_result("身份证错误"), ("not_found", "腾讯历史批次完成、无平台附件"))
        self.assertEqual(historical_result("随手填写的旧备注"), ("found", "腾讯历史批次完成、无平台附件"))


class PhotoSheetOutboxRetryTests(unittest.TestCase):
    def test_retry_uses_exponential_backoff(self):
        now = datetime(2026, 8, 16, 9, 0)

        first = outbox_retry_plan(0, uncertain=False, now=now)
        third = outbox_retry_plan(2, uncertain=False, now=now)

        self.assertEqual(first.status, "retry")
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(first.next_attempt_at, datetime(2026, 8, 16, 9, 5))
        self.assertEqual(third.next_attempt_at, datetime(2026, 8, 16, 9, 20))
        self.assertEqual(third.error_code, "write_failed")

    def test_retry_delay_is_capped_and_uncertain_code_is_preserved(self):
        now = datetime(2026, 8, 16, 9, 0)

        plan = outbox_retry_plan(8, uncertain=True, now=now)

        self.assertEqual(plan.status, "retry")
        self.assertEqual(plan.next_attempt_at, datetime(2026, 8, 16, 15, 0))
        self.assertEqual(plan.error_code, "write_uncertain")

    def test_retry_pauses_after_maximum_automatic_attempts(self):
        plan = outbox_retry_plan(11, uncertain=False)

        self.assertEqual(plan.status, "paused")
        self.assertEqual(plan.attempt_count, 12)
        self.assertIsNone(plan.next_attempt_at)
        self.assertEqual(plan.error_code, "write_failed_exhausted")


class PhotoSheetOutboxCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_relocation_reuses_one_full_source_snapshot(self):
        first_values = ["冬梅社区", "来源甲", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        second_values = ["蠡湖社区", "来源乙", "乙", "320500200001010028", "申请人", "2026/8/11", ""]
        first = parse_rows([source_row(23, first_values)])[0]
        second = parse_rows([source_row(24, second_values)])[0]
        client = type("Client", (), {})()
        client.read_source_row = AsyncMock(side_effect=[
            source_row(10, ["其他社区", "其他", "丙", "", "", "", ""]),
            source_row(11, ["其他社区", "其他", "丁", "", "", "", ""]),
        ])
        client.read_all_source_rows = AsyncMock(return_value=[
            source_row(23, first_values),
            source_row(24, second_values),
        ])
        source = {"file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}
        cache = SourceRowsCache()

        first_cursor = type("Cursor", (), {})()
        first_cursor.execute = AsyncMock()
        first_cursor.fetchone = AsyncMock(return_value=(10, first.fingerprint))
        second_cursor = type("Cursor", (), {})()
        second_cursor.execute = AsyncMock()
        second_cursor.fetchone = AsyncMock(return_value=(11, second.fingerprint))

        first_result = await _locate_mapping(
            client, source, first_cursor, 101, rows_cache=cache,
        )
        second_result = await _locate_mapping(
            client, source, second_cursor, 102, rows_cache=cache,
        )

        self.assertEqual(first_result[0], 23)
        self.assertEqual(second_result[0], 24)
        client.read_all_source_rows.assert_awaited_once()

    async def test_snapshot_load_failure_is_not_retried_inside_same_batch(self):
        client = type("Client", (), {})()
        client.read_all_source_rows = AsyncMock(side_effect=RuntimeError("synthetic timeout"))
        source = {"file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}
        cache = SourceRowsCache()

        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "synthetic timeout"):
                await cache.load(client, source)

        client.read_all_source_rows.assert_awaited_once()

    async def test_old_unbounded_retries_are_paused_before_processing(self):
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.rowcount = 4

        paused = await _pause_exhausted_outbox(cursor)

        self.assertEqual(paused, 4)
        self.assertEqual(cursor.execute.await_count, 2)
        first_sql, first_params = cursor.execute.await_args_list[0].args
        self.assertIn("status='paused'", first_sql)
        self.assertEqual(first_params, (photo_sheet_sync.PHOTO_OUTBOX_MAX_AUTO_ATTEMPTS,))
        second_sql, _ = cursor.execute.await_args_list[1].args
        self.assertIn("external_sync_status='paused'", second_sql)


class PhotoSheetRelocationTests(unittest.IsolatedAsyncioTestCase):
    def test_unique_identity_pairs_a_revised_source_row_without_creating_a_ticket(self):
        incoming = parse_rows([source_row(
            23, ["冬梅社区", "出租房核查", "甲", "32050020000101001X", "申请人", "2026/8/12", ""],
        )])[0]
        previous = ExistingPhotoSheetRow(
            1, 101, 10, "old-fingerprint", identity_hmac=incoming.identity_hmac,
        )

        pairs, remaining = _pair_revised_rows([previous], [incoming], {})

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].previous.work_order_id, 101)
        self.assertEqual(pairs[0].incoming.physical_row, 23)
        self.assertEqual(remaining, [])

    def test_revised_row_with_duplicate_identity_stays_for_manual_review(self):
        incoming = parse_rows([source_row(
            23, ["冬梅社区", "出租房核查", "甲", "32050020000101001X", "申请人", "2026/8/12", ""],
        )])[0]
        previous = ExistingPhotoSheetRow(
            1, 101, 10, "old-fingerprint", identity_hmac=incoming.identity_hmac,
        )
        another_previous = ExistingPhotoSheetRow(
            2, 102, 11, "another-fingerprint", identity_hmac=incoming.identity_hmac,
        )

        pairs, remaining = _pair_revised_rows([previous, another_previous], [incoming], {})

        self.assertEqual(pairs, [])
        self.assertEqual({item.work_order_id for item in remaining}, {101, 102})

    def test_revised_duplicate_prefers_earlier_ticket_with_existing_attachment(self):
        previous = ExistingPhotoSheetRow(
            1, 101, 10, "old", identity_hmac="identity", has_attachment=True,
            work_order_status="completed", created_at=datetime(2026, 8, 10),
        )
        current = ExistingPhotoSheetRow(
            2, 102, 23, "new", identity_hmac="identity", has_attachment=False,
            work_order_status="queued", created_at=datetime(2026, 8, 12),
        )

        canonical = _canonical_revised_mapping(previous, current)

        self.assertEqual(canonical.work_order_id, 101)

    def test_swapped_physical_rows_are_paired_before_unique_rows_are_restored(self):
        first = parse_rows([source_row(
            20, ["冬梅社区", "来源甲", "甲", "32050020000101001X", "申请人", "2026/8/11", ""],
        )])[0]
        second = parse_rows([source_row(
            10, ["蠡湖社区", "来源乙", "乙", "320500200001010028", "申请人", "2026/8/11", ""],
        )])[0]
        existing = [
            ExistingPhotoSheetRow(1, 101, 10, first.fingerprint),
            ExistingPhotoSheetRow(2, 102, 20, second.fingerprint),
        ]

        matches, unmatched = _pair_relocated_rows(existing, [first, second])

        self.assertEqual(matches[20].work_order_id, 101)
        self.assertEqual(matches[10].work_order_id, 102)
        self.assertEqual(unmatched, [])

    def test_duplicate_fingerprints_preserve_one_row_one_ticket_in_row_order(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        incoming = parse_rows([source_row(30, values), source_row(31, values)])
        existing = [
            ExistingPhotoSheetRow(8, 108, 18, incoming[0].fingerprint),
            ExistingPhotoSheetRow(7, 107, 17, incoming[0].fingerprint),
        ]

        matches, unmatched = _pair_relocated_rows(existing, incoming)

        self.assertEqual(matches[30].work_order_id, 107)
        self.assertEqual(matches[31].work_order_id, 108)
        self.assertEqual(unmatched, [])

    def test_extra_incoming_duplicate_is_left_for_new_ticket_creation(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        incoming = parse_rows([source_row(30, values), source_row(31, values)])
        existing = [ExistingPhotoSheetRow(7, 107, 17, incoming[0].fingerprint)]

        matches, unmatched = _pair_relocated_rows(existing, incoming)

        self.assertEqual(set(matches), {30})
        self.assertNotIn(31, matches)
        self.assertEqual(unmatched, [])

    def test_missing_duplicate_keeps_unmatched_mapping_for_missing_status(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        incoming = parse_rows([source_row(30, values)])
        existing = [
            ExistingPhotoSheetRow(7, 107, 17, incoming[0].fingerprint),
            ExistingPhotoSheetRow(8, 108, 18, incoming[0].fingerprint),
        ]

        matches, unmatched = _pair_relocated_rows(existing, incoming)

        self.assertEqual(matches[30].work_order_id, 107)
        self.assertEqual([item.work_order_id for item in unmatched], [108])

    async def test_unique_fingerprint_relocates_moved_row(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        parsed = parse_rows([source_row(23, values)])[0]
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(10, parsed.fingerprint))
        client = type("Client", (), {})()
        client.read_source_row = AsyncMock(return_value=source_row(10, ["其他社区", "其他", "乙", "", "", "", ""]))
        client.read_all_source_rows = AsyncMock(return_value=[source_row(23, values)])
        source = {"file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        physical_row, actual = await _locate_mapping(client, source, cursor, 99)

        self.assertEqual(physical_row, 23)
        self.assertEqual(actual["physical_row"], 23)

    async def test_duplicate_fingerprint_stops_relocation(self):
        values = ["冬梅社区", "来源", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]
        parsed = parse_rows([source_row(23, values)])[0]
        cursor = type("Cursor", (), {})()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(10, parsed.fingerprint))
        client = type("Client", (), {})()
        client.read_source_row = AsyncMock(return_value=source_row(10, ["其他社区", "其他", "乙", "", "", "", ""]))
        client.read_all_source_rows = AsyncMock(return_value=[source_row(23, values), source_row(24, values)])
        source = {"file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        physical_row, actual = await _locate_mapping(client, source, cursor, 99)

        self.assertIsNone(physical_row)
        self.assertIsNone(actual)


class PhotoSheetAppendTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_attempt_appends_after_current_tail_without_full_scan(self):
        detail = ("冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", datetime(2026, 8, 11), "platform")
        cursor = AppendCursor({101: detail})
        client = type("Client", (), {})()
        client.find_last_nonempty_row = AsyncMock(return_value=25)
        client.read_all_source_rows = AsyncMock()
        client.build_update_range_request = Mock(return_value={"fake": "request"})
        client.batch_update = AsyncMock()
        client.read_source_row = AsyncMock(return_value=source_row(
            26, ["冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", "2026/8/11", ""],
        ))
        source = {"id": 7, "file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        await _process_append(client, source, cursor, 101, first_attempt=True)

        self.assertEqual(cursor.mappings[0]["physical_row"], 26)
        client.find_last_nonempty_row.assert_awaited_once_with(
            "fake", "fake-tab", 1, COLUMNS,
        )
        client.read_all_source_rows.assert_not_awaited()
        client.build_update_range_request.assert_called_once_with(
            "fake-tab", 25, 0,
            [["冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", "2026/8/11", ""]],
        )

    async def test_first_attempt_ignores_reserved_blank_tail_rows(self):
        values = [
            "Synthetic Community",
            "Synthetic Source",
            "Synthetic Person",
            "32050020000101001X",
            "Synthetic Requester",
            "2026/8/11",
            "",
        ]
        detail = (*values[:5], datetime(2026, 8, 11), "platform")
        cursor = AppendCursor({101: detail})
        client = type("Client", (), {})()
        client.find_last_nonempty_row = AsyncMock(return_value=12247)
        client.build_update_range_request = Mock(return_value={"fake": "request"})
        client.batch_update = AsyncMock()
        client.read_source_row = AsyncMock(return_value=source_row(12248, values))
        source = {"id": 7, "file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        await _process_append(client, source, cursor, 101, first_attempt=True)

        self.assertEqual(cursor.mappings[0]["physical_row"], 12248)
        client.build_update_range_request.assert_called_once_with(
            "fake-tab", 12247, 0, [values],
        )

    async def test_identical_platform_requests_use_distinct_physical_rows(self):
        detail = ("冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", datetime(2026, 8, 11), "platform")
        cursor = AppendCursor({101: detail, 102: detail})
        rows = []
        client = type("Client", (), {})()
        client.build_update_range_request = Mock(return_value={"fake": "request"})
        client.batch_update = AsyncMock()

        async def read_source_row(_file_id, _sheet_id, physical_row, _columns):
            return source_row(physical_row, ["冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", "2026/8/11", ""])

        client.read_source_row = AsyncMock(side_effect=read_source_row)
        source = {"id": 7, "file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        await _process_append(client, source, cursor, 101, known_rows=rows)
        await _process_append(client, source, cursor, 102, known_rows=rows)

        self.assertEqual([item["physical_row"] for item in cursor.mappings], [2, 3])
        self.assertEqual([row["physical_row"] for row in rows], [2, 3])
        self.assertEqual(client.batch_update.await_count, 2)

    async def test_uncertain_previous_write_adopts_one_unmapped_candidate(self):
        detail = ("冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", datetime(2026, 8, 11), "platform")
        cursor = AppendCursor({101: detail})
        candidate = source_row(25, ["冬梅社区", "平安码", "甲", "32050020000101001X", "申请人", "2026/8/11", ""])
        client = type("Client", (), {})()
        client.build_update_range_request = Mock()
        client.batch_update = AsyncMock()
        client.read_source_row = AsyncMock()
        source = {"id": 7, "file_id": "fake", "sheet_id": "fake-tab", "header_row": 1}

        await _process_append(client, source, cursor, 101, known_rows=[candidate])

        self.assertEqual(cursor.mappings[0]["physical_row"], 25)
        client.batch_update.assert_not_awaited()


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SourceCursor:
    def __init__(self, last_full_sync_date: date):
        self.last_full_sync_date = last_full_sync_date
        self._query = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, query, params=()):
        self._query = query

    async def fetchone(self):
        if "FROM photo_sheet_sources" in self._query:
            return (1, "", "file", "sheet", 1, 1, 1, None, None, 10,
                    self.last_full_sync_date, None, "success", "")
        return None


class _SourceConnection:
    def __init__(self, business_date: date):
        self.source_cursor = _SourceCursor(business_date)

    def cursor(self):
        return self.source_cursor


class _SourcePool:
    def __init__(self, business_date: date):
        self.connection = _SourceConnection(business_date)

    def acquire(self):
        return _Context(self.connection)


class PhotoSheetMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_runs_before_scheduled_outbox_and_no_daily_full_when_current(self):
        business_date = date(2026, 8, 12)
        order: list[str] = []

        async def sync_once(*, full=False, actor_user_id=None):
            order.append("full" if full else "incremental")
            return {"disabled": False}

        async def outbox_once(*args, **kwargs):
            order.append("outbox")
            return {"disabled": False}

        with patch.object(photo_sheet_sync, "sync_online_once", new=sync_once), \
             patch.object(photo_sheet_sync, "process_outbox_once", new=outbox_once), \
             patch.object(photo_sheet_sync, "get_business_date_from_db", new=AsyncMock(return_value=business_date)), \
             patch.object(photo_sheet_sync.db_manager, "get_pool", return_value=_SourcePool(business_date)):
            result = await photo_sheet_sync.run_photo_sheet_maintenance_once()

        self.assertEqual(order, ["incremental", "outbox"])
        self.assertEqual(result["sync"], {"disabled": False})
        self.assertFalse(result["full_sync_scheduled"])

    async def test_incremental_failure_does_not_prevent_outbox(self):
        business_date = date(2026, 8, 12)
        outbox = AsyncMock(return_value={"processed": 1})

        async def failing_sync(*, full=False, actor_user_id=None):
            raise RuntimeError("safe synthetic failure")

        with patch.object(photo_sheet_sync, "sync_online_once", new=failing_sync), \
             patch.object(photo_sheet_sync, "process_outbox_once", new=outbox), \
             patch.object(photo_sheet_sync, "get_business_date_from_db", new=AsyncMock(return_value=business_date)), \
             patch.object(photo_sheet_sync.db_manager, "get_pool", return_value=_SourcePool(business_date)):
            result = await photo_sheet_sync.run_photo_sheet_maintenance_once()

        outbox.assert_awaited_once()
        self.assertIsNone(result["sync"])
        self.assertEqual(result["outbox"], {"processed": 1})

    async def test_due_daily_full_sync_is_launched_without_waiting(self):
        business_date = date(2026, 8, 12)
        old_date = date(2026, 8, 11)
        launch = Mock(return_value=True)

        with patch.object(photo_sheet_sync, "sync_online_once", new=AsyncMock(return_value={})), \
             patch.object(photo_sheet_sync, "process_outbox_once", new=AsyncMock(return_value={})), \
             patch.object(photo_sheet_sync, "get_business_date_from_db", new=AsyncMock(return_value=business_date)), \
             patch.object(photo_sheet_sync.db_manager, "get_pool", return_value=_SourcePool(old_date)), \
             patch.object(photo_sheet_sync, "launch_daily_full_sync", new=launch):
            result = await photo_sheet_sync.run_photo_sheet_maintenance_once()

        launch.assert_called_once_with()
        self.assertTrue(result["full_sync_scheduled"])

    async def test_photo_sheet_scheduler_has_its_own_cancellable_loop(self):
        with patch.object(
            photo_sheet_sync,
            "run_photo_sheet_maintenance_once",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await photo_sheet_sync.run_photo_sheet_scheduler()


if __name__ == "__main__":
    unittest.main()
