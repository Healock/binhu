import unittest
from datetime import date
from unittest.mock import patch

from services.visit_source import VisitSourceError, fetch_rows, workbook_bytes


class VisitSourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_rows_are_scoped_and_standardized(self):
        with patch("services.visit_source.settings.VISIT_SOURCE_MOCK", True):
            result = await fetch_rows("detail", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(next(iter(result["rows"][0].values())), "滨湖新城派出所")
        self.assertTrue(workbook_bytes("detail", result["rows"]).startswith(b"PK"))

    async def test_unconfigured_source_does_not_call_network(self):
        with patch("services.visit_source.settings.VISIT_SOURCE_MOCK", False), patch("services.visit_source.settings.VISIT_SOURCE_BASE_URL", ""):
            with self.assertRaises(VisitSourceError) as raised:
                await fetch_rows("rating", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(raised.exception.code, "not_configured")

    async def test_invalid_source_is_rejected(self):
        with self.assertRaises(VisitSourceError) as raised:
            await fetch_rows("unknown", date(2026, 8, 13), date(2026, 8, 13))
        self.assertEqual(raised.exception.code, "invalid_source")


if __name__ == "__main__":
    unittest.main()
