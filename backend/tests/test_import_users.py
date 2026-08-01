import os
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from tools.import_users import (
    apply_display_name_sync,
    apply_preview,
    build_display_name_sync_preview,
    build_preview,
    read_rows,
    reject_duplicates,
    validate_initial_password,
)


class PreviewCursor:
    def __init__(self, members, users, groups=None, position_groups=None):
        self.members = members
        self.users = users
        self.groups = groups or [
            (10, "flow_post", "流口岗"),
            (20, "internal_business", "内勤业务组"),
            (40, "admin", "管理员"),
        ]
        self.position_groups = position_groups or [
            ("组员", 10),
            ("组长", 10),
            ("基础管控", 20),
        ]
        self.result = []

    async def execute(self, sql, params=None):
        del params
        if "FROM _grid_members AS member" in sql:
            self.result = self.members
        elif "FROM _users AS user" in sql:
            self.result = self.users
        elif "FROM _permission_groups" in sql:
            self.result = self.groups
        elif "FROM _position_permission_groups" in sql:
            self.result = self.position_groups
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return list(self.result)


class UserImportTests(unittest.IsolatedAsyncioTestCase):
    def test_short_initial_password_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "至少需要 8 个字符"):
            validate_initial_password("short")
        validate_initial_password("short", allow_short_password=True)
        with self.assertRaisesRegex(ValueError, "至少需要 5 个字符"):
            validate_initial_password("tiny", allow_short_password=True)

    def test_private_xlsx_requires_username_and_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["用户名", "姓名", "职位"])
            sheet.append(["zhangsan", "张三", "流口岗"])
            workbook.save(path)
            workbook.close()

            self.assertEqual(
                read_rows(path),
                [{
                    "username": "zhangsan",
                    "name": "张三",
                    "position": "流口岗",
                }],
            )

    def test_duplicate_username_or_person_stops_whole_batch(self):
        with self.assertRaisesRegex(ValueError, "用户名重复"):
            reject_duplicates([
                {"username": "same", "name": "张三", "position": "流口岗"},
                {"username": "same", "name": "李四", "position": "流口岗"},
            ])
        with self.assertRaisesRegex(ValueError, "姓名重复"):
            reject_duplicates([
                {"username": "one", "name": "张三", "position": "流口岗"},
                {"username": "two", "name": "张三", "position": "流口岗"},
            ])

    def test_blank_position_is_deferred_until_database_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["用户名", "姓名", "职位"])
            sheet.append(["existing", "已有账号", ""])
            workbook.save(path)
            workbook.close()

            self.assertEqual(
                read_rows(path),
                [{
                    "username": "existing",
                    "name": "已有账号",
                    "position": "",
                }],
            )

    async def test_preview_distinguishes_new_and_existing_accounts(self):
        members = [
            (1, "张三", "组员", "长板", 10, "流口岗", "community", "flow_post"),
            (2, "李四", "基础管控", "内勤", 20, "内勤业务组", "internal", "internal_business"),
        ]
        users = [(8, "existing", None, "member", None, "inherited", None)]
        result = await build_preview(
            PreviewCursor(members, users),
            [
                {"username": "new-user", "name": "张三", "position": "流口岗"},
                {"username": "existing", "name": "李四", "position": "内勤岗"},
            ],
        )

        self.assertEqual(
            [item["action"] for item in result],
            ["create", "skip_existing"],
        )
        self.assertEqual(result[0]["department"], "长板")
        self.assertEqual(result[1]["department"], "保持原账号不变")

    async def test_preview_never_changes_existing_account(self):
        members = [
            (1, "张三", "组员", "长板", 10, "流口岗", "community", "flow_post"),
        ]
        users = [
            (8, "existing", None, "admin", 40, "custom", "管理员"),
        ]

        result = await build_preview(
            PreviewCursor(members, users),
            [{"username": "existing", "name": "张三", "position": ""}],
        )

        self.assertEqual(result[0]["action"], "skip_existing")
        self.assertEqual(result[0]["permission_group_id"], 40)
        self.assertEqual(result[0]["permission_group"], "管理员")

    async def test_unmatched_management_positions_use_explicit_groups(self):
        result = await build_preview(
            PreviewCursor([], []),
            [
                {"username": "police", "name": "甲", "position": "社区民警"},
                {"username": "leader", "name": "乙", "position": "所（队）领导"},
                {"username": "office", "name": "丙", "position": "内勤岗"},
            ],
        )

        self.assertEqual(
            [item["permission_group"] for item in result],
            ["管理员", "管理员", "内勤业务组"],
        )
        self.assertTrue(all(item["member_id"] is None for item in result))
        self.assertTrue(
            all(item["assignment_mode"] == "custom" for item in result)
        )
        self.assertEqual(
            [item["legacy_role"] for item in result],
            ["admin", "admin", "admin"],
        )

    async def test_unmatched_flow_post_creates_unassigned_member(self):
        result = await build_preview(
            PreviewCursor([], []),
            [{"username": "flow", "name": "甲", "position": "流口岗"}],
        )

        self.assertEqual(result[0]["action"], "create_placeholder")
        self.assertEqual(result[0]["position"], "组员")
        self.assertEqual(result[0]["department"], "待分配部门")
        self.assertEqual(result[0]["permission_group"], "流口岗")
        self.assertEqual(result[0]["assignment_mode"], "inherited")

    async def test_preview_stops_for_unmatched_or_invalid_department(self):
        members = [
            (1, "张三", "组员", "内勤", 10, "流口岗", "internal", "flow_post"),
        ]
        with self.assertRaisesRegex(ValueError, "组长或组员尚未选择社区部门"):
            await build_preview(
                PreviewCursor(members, []),
                [{"username": "zhangsan", "name": "张三", "position": "流口岗"}],
            )
        with self.assertRaisesRegex(ValueError, "没有导入规则"):
            await build_preview(
                PreviewCursor([], []),
                [{"username": "missing", "name": "不存在", "position": "未知岗位"}],
            )

    async def test_apply_preview_inserts_placeholder_before_account(self):
        class ApplyCursor:
            def __init__(self):
                self.calls = []
                self.lastrowid = 88

            async def execute(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))

        cursor = ApplyCursor()
        await apply_preview(
            cursor,
            [
                {
                    "action": "skip_existing",
                    "username": "old",
                    "member_id": None,
                    "name": "旧账号",
                    "position": "社区民警",
                    "department": "保持原账号不变",
                    "permission_group_id": 40,
                    "permission_group_code": None,
                    "permission_group": "管理员",
                    "assignment_mode": "custom",
                    "legacy_role": "admin",
                    "create_member": False,
                },
                {
                    "action": "create_placeholder",
                    "username": "flow",
                    "member_id": None,
                    "name": "甲",
                    "position": "组员",
                    "department": "待分配部门",
                    "permission_group_id": 10,
                    "permission_group_code": "flow_post",
                    "permission_group": "流口岗",
                    "assignment_mode": "inherited",
                    "legacy_role": "member",
                    "create_member": True,
                },
            ],
            "hashed-password",
        )

        self.assertEqual(len(cursor.calls), 2)
        self.assertIn("INSERT INTO _grid_members", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], ("甲", "组员"))
        self.assertIn("INSERT INTO _users", cursor.calls[1][0])
        self.assertEqual(cursor.calls[1][1][4], 88)

    async def test_new_accounts_store_their_display_name(self):
        class ApplyCursor:
            def __init__(self):
                self.calls = []
                self.lastrowid = 1

            async def execute(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))

        cursor = ApplyCursor()
        await apply_preview(cursor, [{
            "action": "create_unlinked",
            "username": "police",
            "member_id": None,
            "name": "社区民警甲",
            "position": "社区民警",
            "department": "不关联人员资料",
            "permission_group_id": 40,
            "permission_group_code": "admin",
            "permission_group": "管理员",
            "assignment_mode": "custom",
            "legacy_role": "admin",
            "create_member": False,
        }], "hashed-password")

        self.assertIn("display_name", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1][1], "社区民警甲")

    async def test_display_name_sync_only_fills_blank_names(self):
        class SyncCursor:
            def __init__(self):
                self.result = []
                self.updates = []

            async def execute(self, sql, params=None):
                if sql.startswith("SELECT id, username"):
                    self.result = [
                        (1, "blank", ""),
                        (2, "kept", "已有姓名"),
                    ]
                else:
                    self.updates.append((" ".join(sql.split()), params))

            async def fetchall(self):
                return list(self.result)

        cursor = SyncCursor()
        preview = await build_display_name_sync_preview(cursor, [
            {"username": "blank", "name": "待补姓名", "position": ""},
            {"username": "kept", "name": "名单姓名", "position": ""},
        ])
        self.assertEqual(
            [item["action"] for item in preview],
            ["fill_name", "skip_populated"],
        )

        await apply_display_name_sync(cursor, preview)
        self.assertEqual(len(cursor.updates), 1)
        self.assertEqual(cursor.updates[0][1], ("待补姓名", 1))

    async def test_display_name_sync_rejects_unknown_accounts(self):
        class EmptyCursor:
            async def execute(self, *_):
                return None

            async def fetchall(self):
                return []

        with self.assertRaisesRegex(ValueError, "1 个用户名尚未创建"):
            await build_display_name_sync_preview(EmptyCursor(), [{
                "username": "missing", "name": "未创建", "position": "",
            }])


if __name__ == "__main__":
    unittest.main()
