import os
import unittest

from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.permission_groups import _normalize_mapping_values
from routers.users import _replace_custom_group_links, _resolve_groups
from routers.grid_members import _resolve_department
from services.permissions import POSITION_DEFAULT_GROUP
from services.personnel_positions import POSITION_CATEGORIES


class GroupCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.many_calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def executemany(self, sql, rows):
        self.many_calls.append((" ".join(sql.split()), list(rows)))

    async def fetchall(self):
        return list(self.rows)

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class MultiPermissionGroupTests(unittest.IsolatedAsyncioTestCase):
    def test_new_positions_have_categories_and_default_groups(self):
        self.assertIn("社区民警", POSITION_CATEGORIES["police_leadership"])
        self.assertIn("所队领导", POSITION_CATEGORIES["police_leadership"])
        self.assertEqual(POSITION_DEFAULT_GROUP["社区民警"], "admin")
        self.assertEqual(POSITION_DEFAULT_GROUP["所队领导"], "admin")

    async def test_community_police_requires_department_and_leader_uses_internal(self):
        with self.assertRaises(HTTPException):
            await _resolve_department(GroupCursor(), "社区民警", None)

        department_id, community = await _resolve_department(
            GroupCursor([(9,)]),
            "所队领导",
            None,
        )
        self.assertEqual((department_id, community), (9, ""))

    def test_position_mapping_accepts_old_single_id_and_new_arrays(self):
        self.assertEqual(
            _normalize_mapping_values({"组员": 10, "组长": [10, 20, 10]}),
            {"组员": [10], "组长": [10, 20]},
        )

    async def test_super_admin_group_cannot_be_mixed(self):
        cursor = GroupCursor([
            (10, "flow_post", "流口岗", 10),
            (50, "super_admin", "超级管理员", 50),
        ])
        with self.assertRaises(HTTPException) as raised:
            await _resolve_groups(
                cursor,
                member_id=None,
                assignment_mode="custom",
                permission_group_id=None,
                permission_group_ids=[10, 50],
            )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_custom_group_links_replace_all_selected_groups(self):
        cursor = GroupCursor()

        await _replace_custom_group_links(
            cursor,
            user_id=7,
            assignment_mode="custom",
            group_ids=[10, 20],
        )

        self.assertIn("DELETE FROM _user_permission_group_links", cursor.calls[0][0])
        self.assertEqual(cursor.many_calls[0][1], [(7, 10), (7, 20)])


if __name__ == "__main__":
    unittest.main()
