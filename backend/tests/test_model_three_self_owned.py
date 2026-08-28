import io
import os
import zipfile
import unittest

from openpyxl import Workbook

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.model_three_self_owned import (
    SelfOwnedImportError,
    _public_batch,
    parse_self_owned_zip,
    should_apply_self_owned_result,
)


def _xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["\ufeff社区名称", "居民证号", "住房类型", "居住处所"])
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class ModelThreeSelfOwnedTests(unittest.TestCase):
    def test_parses_multiple_workbooks_deduplicates_and_counts_invalid_rows(self):
        content = _zip_bytes({
            "one.xlsx": _xlsx_bytes([
                ["测试社区", "11010519491231002X", "自购自住", "自购自住"],
                ["测试社区", "11010519491231002X", "自购自住", "自购自住"],
                ["测试社区", "bad", "自购自住", "自购自住"],
            ]),
            "two.xlsx": _xlsx_bytes([
                ["测试社区", "110105194912310011", "自购自住", "自购自住"],
            ]),
        })
        parsed = parse_self_owned_zip(content)
        self.assertEqual(parsed.workbook_count, 2)
        self.assertEqual(parsed.total_rows, 4)
        self.assertEqual(parsed.valid_rows, 2)
        self.assertEqual(parsed.invalid_rows, 1)
        self.assertEqual(parsed.duplicate_rows, 1)
        self.assertEqual(len(parsed.identities), 2)

    def test_requires_identity_column_and_valid_record(self):
        content = _zip_bytes({"bad.xlsx": _xlsx_bytes([["社区", "", "", ""]])})
        with self.assertRaisesRegex(SelfOwnedImportError, "没有有效身份证"):
            parse_self_owned_zip(content)

    def test_only_blank_or_unverifiable_result_can_be_filled(self):
        self.assertTrue(should_apply_self_owned_result("", False))
        self.assertTrue(should_apply_self_owned_result("无法核实", False))
        self.assertFalse(should_apply_self_owned_result("近期返吴", False))
        self.assertFalse(should_apply_self_owned_result("在吴", False))
        self.assertFalse(should_apply_self_owned_result("", True))

    def test_public_batch_does_not_expose_internal_creator_id(self):
        result = _public_batch((
            1, "名单.zip", "a" * 64, "self-owned-v1", "completed", 1,
            2, 2, 0, 0, 1, 1, 0, 99, None, None, "",
        ))
        self.assertNotIn("created_by", result)
        self.assertEqual(result["updated_tasks"], 1)


if __name__ == "__main__":
    unittest.main()
