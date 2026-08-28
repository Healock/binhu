import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.local_source import (
    LOCAL_SPREADSHEET_ID,
    local_data_source_enabled,
    local_row_hash,
    local_sheet_id,
    stable_json,
)
from config import settings


class LocalSourceHelpersTest(unittest.TestCase):
    def test_local_locator_is_stable_and_not_a_tencent_position(self):
        self.assertEqual(LOCAL_SPREADSHEET_ID, 0)
        self.assertEqual(local_sheet_id("全链条"), "local:全链条")

    def test_stable_json_and_hash_are_order_independent(self):
        left = {"身份证号": "320000000000000000", "社区": "长板"}
        right = {"社区": "长板", "身份证号": "320000000000000000"}
        self.assertEqual(stable_json(left), stable_json(right))
        self.assertEqual(local_row_hash(left), local_row_hash(right))

    def test_feature_switch_is_read_from_settings(self):
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", True):
            self.assertTrue(local_data_source_enabled())
        with patch.object(settings, "LOCAL_DATA_SOURCE_ENABLED", False):
            self.assertFalse(local_data_source_enabled())


if __name__ == "__main__":
    unittest.main()
