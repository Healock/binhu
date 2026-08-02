import unittest
from unittest.mock import AsyncMock, call

from services.txdocs_client import TxDocsClient


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


if __name__ == "__main__":
    unittest.main()
