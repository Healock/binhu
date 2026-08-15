from __future__ import annotations

import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.registry_extended import (
    REGISTRY_IMPORT_WRITE_CHUNK,
    _bulk_insert_import_issues,
    _bulk_insert_source_records,
    _household_source_ref,
)


class _Cursor:
    def __init__(self):
        self.calls: list[tuple[str, list[tuple]]] = []

    async def executemany(self, sql, values):
        self.calls.append((sql, list(values)))


class RegistryImportBatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_preview_rows_are_written_in_bounded_batches(self):
        cursor = _Cursor()
        rows = [(1, str(index), "household_property", "{}") for index in range(1201)]

        await _bulk_insert_source_records(cursor, rows)

        self.assertEqual([len(values) for _, values in cursor.calls], [
            REGISTRY_IMPORT_WRITE_CHUNK,
            REGISTRY_IMPORT_WRITE_CHUNK,
            201,
        ])
        self.assertTrue(all("registry_source_records" in sql for sql, _ in cursor.calls))

    async def test_empty_issue_batch_does_not_send_sql(self):
        cursor = _Cursor()

        await _bulk_insert_import_issues(cursor, [])

        self.assertEqual(cursor.calls, [])

    def test_household_source_ref_keeps_sheet_and_physical_row(self):
        self.assertEqual(
            _household_source_ref({"source_sheet": "长板社区", "source_row": 25}),
            "长板社区:25",
        )


if __name__ == "__main__":
    unittest.main()
