import os
import unittest

from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.grid_members import GridMemberCreate, _attach_account_to_new_member
from services.member_departments import (
    replace_member_departments,
    resolve_departments,
)


class Cursor:
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


class AccountCursor(Cursor):
    def __init__(self, account):
        super().__init__()
        self.account = account
        self.current = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "FROM _position_permission_group_links" in normalized:
            self.current = [(10, "flow_post", "流口岗")]
        elif "FROM _users" in normalized and "FOR UPDATE" in normalized:
            self.current = [self.account] if self.account else []
        else:
            self.current = []

    async def fetchone(self):
        return self.current[0] if self.current else None


class MemberDepartmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_community_police_can_select_multiple_communities(self):
        rows = [
            (2, "南厍", "community", "南厍"),
            (3, "阅湖", "community", "阅湖"),
        ]
        departments = await resolve_departments(Cursor(rows), "社区民警", [2, 3])
        self.assertEqual([item["community_name"] for item in departments], ["南厍", "阅湖"])
        with self.assertRaises(HTTPException) as raised:
            await resolve_departments(Cursor(rows), "组员", [2, 3])
        self.assertEqual(raised.exception.status_code, 400)

    async def test_replacing_departments_updates_links_and_legacy_primary(self):
        cursor = Cursor()
        await replace_member_departments(cursor, 7, [
            {"id": 2, "community_name": "南厍"},
            {"id": 3, "community_name": "阅湖"},
        ])
        self.assertIn("DELETE FROM _grid_member_department_links", cursor.calls[0][0])
        self.assertEqual(cursor.many_calls[0][1], [(7, 2, 0), (7, 3, 1)])
        self.assertEqual(cursor.calls[-1][1], (2, "南厍", 7))

    def test_new_member_requires_account_details(self):
        with self.assertRaises(ValidationError):
            GridMemberCreate(name="测试", position="组员")
        payload = GridMemberCreate(
            name="测试",
            position="组员",
            department_ids=[2],
            account_mode="create",
            username="tester",
            password="password-123",
        )
        self.assertEqual(payload.account_mode, "create")

        with self.assertRaises(ValidationError):
            GridMemberCreate(
                name="测试",
                position="组员",
                department_ids=[2],
                account_mode="create",
                username="  ",
                password="password-123",
            )

    async def test_existing_account_is_rebound_to_inherited_position_permissions(self):
        payload = GridMemberCreate(
            name="测试",
            position="组员",
            department_ids=[2],
            account_mode="existing",
            existing_user_id=8,
        )
        cursor = AccountCursor((8, "tester", "admin", None))

        account_id, username = await _attach_account_to_new_member(
            cursor,
            payload,
            member_id=5,
        )

        self.assertEqual((account_id, username), (8, "tester"))
        update = next(call for call in cursor.calls if call[0].startswith("UPDATE _users"))
        self.assertIn("group_assignment_mode='inherited'", update[0])
        self.assertEqual(update[1], (5, "测试", 10, "member", 8))

    async def test_account_already_linked_to_another_member_is_rejected(self):
        payload = GridMemberCreate(
            name="测试",
            position="组员",
            department_ids=[2],
            account_mode="existing",
            existing_user_id=8,
        )
        with self.assertRaises(HTTPException) as raised:
            await _attach_account_to_new_member(
                AccountCursor((8, "tester", "member", 99)),
                payload,
                member_id=5,
            )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
