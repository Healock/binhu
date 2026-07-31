import os
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from tools.import_users import build_preview, read_rows, reject_duplicates


class PreviewCursor:
    def __init__(self, members, users):
        self.members = members
        self.users = users
        self.result = []

    async def execute(self, sql, params=None):
        del params
        if "FROM _grid_members AS member" in sql:
            self.result = self.members
        elif "FROM _users AS user" in sql:
            self.result = self.users
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return list(self.result)


class UserImportTests(unittest.IsolatedAsyncioTestCase):
    def test_private_xlsx_requires_username_and_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["用户名", "姓名"])
            sheet.append(["zhangsan", "张三"])
            workbook.save(path)
            workbook.close()

            self.assertEqual(
                read_rows(path),
                [{"username": "zhangsan", "name": "张三"}],
            )

    def test_duplicate_username_or_person_stops_whole_batch(self):
        with self.assertRaisesRegex(ValueError, "用户名重复"):
            reject_duplicates([
                {"username": "same", "name": "张三"},
                {"username": "same", "name": "李四"},
            ])
        with self.assertRaisesRegex(ValueError, "姓名重复"):
            reject_duplicates([
                {"username": "one", "name": "张三"},
                {"username": "two", "name": "张三"},
            ])

    async def test_preview_distinguishes_new_and_existing_accounts(self):
        members = [
            (1, "张三", "组员", "长板", 10, "流口岗", "community"),
            (2, "李四", "基础管控", "内勤", 20, "内勤业务组", "internal"),
        ]
        users = [(8, "existing", None, "member", None, "inherited", None)]
        result = await build_preview(
            PreviewCursor(members, users),
            [
                {"username": "new-user", "name": "张三"},
                {"username": "existing", "name": "李四"},
            ],
        )

        self.assertEqual([item["action"] for item in result], ["create", "link"])
        self.assertEqual(result[0]["department"], "长板")
        self.assertEqual(result[1]["permission_group"], "内勤业务组")

    async def test_preview_keeps_existing_custom_group(self):
        members = [
            (1, "张三", "组员", "长板", 10, "流口岗", "community"),
        ]
        users = [
            (8, "existing", None, "admin", 40, "custom", "管理员"),
        ]

        result = await build_preview(
            PreviewCursor(members, users),
            [{"username": "existing", "name": "张三"}],
        )

        self.assertEqual(result[0]["action"], "link_keep_group")
        self.assertEqual(result[0]["permission_group_id"], 40)
        self.assertEqual(result[0]["permission_group"], "管理员")

    async def test_preview_stops_for_unmatched_or_invalid_department(self):
        members = [
            (1, "张三", "组员", "内勤", 10, "流口岗", "internal"),
        ]
        with self.assertRaisesRegex(ValueError, "组长或组员尚未选择社区部门"):
            await build_preview(
                PreviewCursor(members, []),
                [{"username": "zhangsan", "name": "张三"}],
            )
        with self.assertRaisesRegex(ValueError, "找不到人员"):
            await build_preview(
                PreviewCursor([], []),
                [{"username": "missing", "name": "不存在"}],
            )


if __name__ == "__main__":
    unittest.main()
