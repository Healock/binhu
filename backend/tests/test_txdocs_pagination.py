import os
import unittest
from unittest.mock import AsyncMock, call

import httpx

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.txdocs_client import TxDocsAPIError, TxDocsClient


def make_response(row_count: int, column_count: int) -> dict:
    return {
        "gridData": {
            "rows": [
                {
                    "values": [
                        {"cellValue": {"text": f"row-{row_index}-col-{col_index}"}}
                        for col_index in range(column_count)
                    ]
                }
                for row_index in range(row_count)
            ]
        }
    }


class TxDocsPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nine_column_table_is_limited_to_one_thousand_rows(self):
        client = TxDocsClient("client", "token", "user")
        client.read_range = AsyncMock(
            side_effect=[
                make_response(1000, 9),
                make_response(2, 9),
            ]
        )
        columns = [f"column-{index}" for index in range(9)]

        rows = await client.read_all_data(
            "file",
            "sheet",
            header_row=1,
            column_names=columns,
        )

        self.assertEqual(len(rows), 1002)
        self.assertEqual(
            client.read_range.await_args_list,
            [
                call("file", "sheet", "A2:I1001"),
                call("file", "sheet", "A1002:I2001"),
            ],
        )

    async def test_wide_table_still_obeys_cell_limit(self):
        client = TxDocsClient("client", "token", "user")
        client.read_range = AsyncMock(return_value=make_response(0, 14))
        columns = [f"column-{index}" for index in range(14)]

        rows = await client.read_all_data(
            "file",
            "sheet",
            header_row=1,
            column_names=columns,
        )

        self.assertEqual(rows, [])
        client.read_range.assert_awaited_once_with(
            "file",
            "sheet",
            "A2:N715",
        )

    async def test_blank_rows_do_not_end_pagination_or_shift_physical_rows(self):
        first_page = make_response(1000, 2)
        first_page["gridData"]["rows"][0] = {"values": []}
        client = TxDocsClient("client", "token", "user")
        client.read_range = AsyncMock(side_effect=[
            first_page,
            make_response(1, 2),
        ])

        rows = await client.read_all_source_rows(
            "file",
            "sheet",
            header_row=1,
            column_names=["first", "second"],
        )

        self.assertEqual(len(rows), 1000)
        self.assertEqual(rows[0]["physical_row"], 3)
        self.assertEqual(rows[-1]["physical_row"], 1002)
        self.assertEqual(
            client.read_range.await_args_list,
            [
                call("file", "sheet", "A2:B1001"),
                call("file", "sheet", "A1002:B2001"),
            ],
        )

    async def test_repeated_header_inside_data_range_is_skipped(self):
        client = TxDocsClient("client", "token", "user")
        response = make_response(2, 3)
        response["gridData"]["rows"][0] = {
            "values": [
                {"cellValue": {"text": "核查人"}},
                {"cellValue": {"text": "姓名"}},
                {"cellValue": {"text": "社区"}},
            ]
        }
        response["gridData"]["rows"][1] = {
            "values": [
                {"cellValue": {"text": "网格员甲"}},
                {"cellValue": {"text": "对象乙"}},
                {"cellValue": {"text": "长板"}},
            ]
        }
        client.read_range = AsyncMock(return_value=response)

        rows = await client.read_all_source_rows(
            "file", "sheet", 1, ["核查人", "姓名", "社区"]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["physical_row"], 3)
        self.assertEqual(rows[0]["values"]["社区"], "长板")

    async def test_detected_header_can_be_retained_for_append_position(self):
        client = TxDocsClient("client", "token", "user")
        response = {
            "gridData": {
                "rows": [{
                    "values": [
                        {"cellValue": {"text": "核查人"}},
                        {"cellValue": {"text": "姓名"}},
                        {"cellValue": {"text": "社区"}},
                    ]
                }]
            }
        }
        client.read_range = AsyncMock(return_value=response)

        rows = await client.read_all_source_rows(
            "file", "sheet", 1, ["核查人", "姓名", "社区"],
            include_detected_headers=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_header"])
        self.assertEqual(rows[0]["physical_row"], 2)

    def test_numeric_month_day_keeps_trailing_zero(self):
        client = TxDocsClient("client", "token", "user")
        values, metadata = client._decode_row(
            {
                "values": [
                    {"cellValue": {"number": 7.3}},
                    {"cellValue": {"number": 7.03}},
                ]
            },
            ["下发时间", "截止时间"],
        )

        self.assertEqual(values, {"下发时间": "7.30", "截止时间": "7.03"})
        self.assertEqual(metadata["下发时间"]["type"], "number")

    async def test_http_200_business_error_is_not_treated_as_success(self):
        async def handler(_request):
            return httpx.Response(
                200,
                json={"code": 400001, "message": "请求参数错误"},
            )

        http = httpx.AsyncClient(
            base_url="https://docs.qq.com",
            transport=httpx.MockTransport(handler),
        )
        client = TxDocsClient("client", "token", "user", http_client=http)
        try:
            with self.assertRaisesRegex(TxDocsAPIError, "400001.*请求参数错误"):
                await client.batch_update("file", [{"unsupportedRequest": {}}])
        finally:
            await http.aclose()


if __name__ == "__main__":
    unittest.main()
