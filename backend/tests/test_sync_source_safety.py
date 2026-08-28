import unittest
from unittest.mock import AsyncMock, patch

from services.parsers.fullchain import FullChainParser
from services.sync_engine import (
    SyncEngine,
    finalize_source_projection,
    source_read_requires_confirmation,
    source_rows_digest,
)


class FakeCursor:
    def __init__(self, row_count):
        self.row_count = row_count

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        del sql, params

    async def fetchone(self):
        return (self.row_count,)


class FakeConnection:
    def __init__(self, row_count):
        self.cursor_instance = FakeCursor(row_count)

    def cursor(self):
        return self.cursor_instance


class TransactionConnection(FakeConnection):
    def __init__(self):
        super().__init__(0)
        self.events = []

    async def begin(self):
        self.events.append("begin")

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


def source_row(physical_row: int, address: str) -> dict:
    parser = FullChainParser()
    values = {column: "" for column in parser.COLUMNS}
    values.update({
        "下发日期": "2026-08-10",
        "身份证号": f"identity-{physical_row}",
        "电话号码": f"phone-{physical_row}",
        "地址": address,
    })
    return {
        "physical_row": physical_row,
        "values": values,
        "cell_meta": {},
    }


class SourceReadSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_confirmation_thresholds(self):
        self.assertTrue(source_read_requires_confirmation(3, 0))
        self.assertTrue(source_read_requires_confirmation(100, 49))
        self.assertFalse(source_read_requires_confirmation(100, 50))
        self.assertFalse(source_read_requires_confirmation(19, 9))
        self.assertFalse(source_read_requires_confirmation(0, 0))

    def test_digest_is_stable_by_physical_row(self):
        parser = FullChainParser()
        first = [source_row(2, "地址二"), source_row(1, "地址一")]
        second = list(reversed(first))
        self.assertEqual(
            source_rows_digest(parser, first),
            source_rows_digest(parser, second),
        )

    async def test_normal_read_does_not_repeat(self):
        rows = [source_row(index, f"地址{index}") for index in range(1, 91)]
        client = AsyncMock()
        client.read_all_source_rows = AsyncMock(return_value=rows)
        result = await SyncEngine(None)._read_source_rows_safely(
            FakeConnection(100),
            client,
            {
                "id": 1,
                "name": "全链条",
                "file_id": "file",
                "data_sheet_id": "sheet",
                "header_row": 1,
            },
            FullChainParser.COLUMNS,
            FullChainParser(),
        )
        self.assertEqual(result, rows)
        self.assertEqual(client.read_all_source_rows.await_count, 1)

    async def test_matching_second_empty_read_is_accepted(self):
        client = AsyncMock()
        client.read_all_source_rows = AsyncMock(side_effect=[[], []])
        result = await SyncEngine(None)._read_source_rows_safely(
            FakeConnection(3),
            client,
            {
                "id": 1,
                "name": "全链条",
                "file_id": "file",
                "data_sheet_id": "sheet",
                "header_row": 1,
            },
            FullChainParser.COLUMNS,
            FullChainParser(),
        )
        self.assertEqual(result, [])
        self.assertEqual(client.read_all_source_rows.await_count, 2)

    async def test_inconsistent_second_read_stops_before_mutation(self):
        first = [source_row(1, "地址一")]
        second = [source_row(1, "地址二")]
        client = AsyncMock()
        client.read_all_source_rows = AsyncMock(side_effect=[first, second])

        with self.assertRaisesRegex(RuntimeError, "未修改缓存或归档"):
            await SyncEngine(None)._read_source_rows_safely(
                FakeConnection(100),
                client,
                {
                    "id": 1,
                    "name": "全链条",
                    "file_id": "file",
                    "data_sheet_id": "sheet",
                    "header_row": 1,
                },
                FullChainParser.COLUMNS,
                FullChainParser(),
            )

    async def test_final_projection_refresh_is_committed_atomically(self):
        conn = TransactionConnection()
        mark = AsyncMock()
        rebuild = AsyncMock()
        reconcile = AsyncMock()
        reconcile_reviews = AsyncMock()
        wake_review = patch("services.unverifiable_review.wake_unverifiable_review_scheduler")
        with (
            patch("services.sync_engine.mark_writebacks_synced", mark),
            patch("services.sync_engine.rebuild_projection", rebuild),
            patch(
                "services.sync_engine.reconcile_police_dispatch_publications",
                reconcile,
            ),
            patch(
                "services.unverifiable_review.reconcile_unverifiable_source_contexts",
                reconcile_reviews,
            ),
            wake_review as wake,
        ):
            await finalize_source_projection(
                conn,
                spreadsheet={"id": 7, "parser_type": "全链条"},
                source_columns=["身份证号"],
            )
        self.assertEqual(conn.events, ["begin", "commit"])
        reconcile.assert_awaited_once()
        mark.assert_awaited_once()
        rebuild.assert_awaited_once()
        reconcile_reviews.assert_awaited_once()
        wake.assert_called_once_with()

    async def test_final_projection_refresh_rolls_back_as_one_unit(self):
        conn = TransactionConnection()
        with (
            patch("services.sync_engine.mark_writebacks_synced", AsyncMock()),
            patch(
                "services.sync_engine.rebuild_projection",
                AsyncMock(side_effect=RuntimeError("fictional rebuild failure")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "fictional rebuild failure"):
                await finalize_source_projection(
                    conn,
                    spreadsheet={"id": 7, "parser_type": "出租房屋核查"},
                    source_columns=[],
                )
        self.assertEqual(conn.events, ["begin", "rollback"])


if __name__ == "__main__":
    unittest.main()
