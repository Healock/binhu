import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from services.photo_sheet_sync import (
    COLUMNS,
    normalize_import_identity,
    historical_result,
    _locate_mapping,
    _process_append,
    parse_rows,
    parse_source_url,
    preview_summary,
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


def source_row(number: int, values: list[str]) -> dict:
    return {
        "physical_row": number,
        "values": dict(zip(COLUMNS, values)),
    }


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

    def test_historical_g_failure_note_only_changes_result_classification(self):
        self.assertEqual(historical_result("身份证错误"), ("not_found", "腾讯历史批次完成、无平台附件"))
        self.assertEqual(historical_result("随手填写的旧备注"), ("found", "腾讯历史批次完成、无平台附件"))


class PhotoSheetRelocationTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
