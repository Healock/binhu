import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.grid_members import (
    GridMemberCreate,
    GridMemberUpdate,
    _member_to_dict,
    _reassign_member_account,
    create_member,
    update_member,
)


class CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.began = False
        self.committed = False
        self.rolled_back = False

    async def begin(self):
        self.began = True

    def cursor(self):
        return CursorContext(self.cursor_instance)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class IdentityUpdateCursor:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.current = []
        self.calls = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if normalized.startswith("SELECT status, leave_start_date"):
            self.current = [("在岗", None, None, "组员", 2)]
        elif "WHERE id_card_number=%s AND id<>%s" in normalized:
            self.current = [(99,)] if self.duplicate else []
        else:
            self.current = []

    async def fetchone(self):
        return self.current[0] if self.current else None


class ReassignmentCursor:
    def __init__(self, target_member_id=2, target_role="member"):
        self.target_member_id = target_member_id
        self.target_role = target_role
        self.current = []
        self.calls = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if normalized.startswith("SELECT id, username, role, member_id FROM _users"):
            self.current = [
                (10, "old-account", "member", 1),
                (20, "target-account", self.target_role, self.target_member_id),
            ]
        elif normalized.startswith("SELECT id, name, position FROM _grid_members"):
            self.current = [(1, "人员甲", "组员")]
            if self.target_member_id is not None:
                self.current.append((self.target_member_id, "人员乙", "基础管控"))
        elif "FROM _position_permission_group_links" in normalized:
            position = params[0]
            self.current = (
                [(11, "internal_business", "内勤业务组")]
                if position == "基础管控"
                else [(10, "flow_post", "流口岗")]
            )
        else:
            self.current = []

    async def fetchall(self):
        return list(self.current)

    async def fetchone(self):
        return self.current[0] if self.current else None


class GridMemberIdentityAccountTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_is_normalized_and_validated(self):
        payload = GridMemberUpdate(id_card_number=" 99999919990101999x ")
        self.assertEqual(payload.id_card_number, "99999919990101999X")
        with self.assertRaises(ValidationError):
            GridMemberUpdate(id_card_number="not-an-identity")

    def test_identity_fields_are_only_serialized_for_super_admin(self):
        row = (
            1, "人员甲", "南厍", "组员", "13800000000", "备注", "在岗",
            None, None, "", "manual", "99999919990101999X", None, None, None, None,
        )
        hidden = _member_to_dict(
            row,
            date(2026, 8, 2),
            sensitive=True,
            identity_access=False,
        )
        self.assertNotIn("has_id_card", hidden)
        self.assertNotIn("id_card_masked", hidden)
        self.assertNotIn("id_card_number", hidden)

        visible = _member_to_dict(
            row,
            date(2026, 8, 2),
            sensitive=True,
            identity_access=True,
        )
        self.assertTrue(visible["has_id_card"])
        self.assertEqual(visible["id_card_number"], "99999919990101999X")

    def test_phone_is_visible_without_sensitive_permission(self):
        row = (
            1, "人员甲", "南厍", "组员", "13800000000", "内部备注", "在岗",
            None, None, "", "manual", None, None, None, None, None,
        )

        result = _member_to_dict(
            row,
            date(2026, 8, 10),
            sensitive=False,
            identity_access=False,
        )

        self.assertEqual(result["phone"], "13800000000")
        self.assertNotIn("notes", result)
        self.assertNotIn("leave_reason", result)
        self.assertNotIn("id_card_number", result)

    async def test_non_super_admin_cannot_create_or_update_identity(self):
        create_payload = GridMemberCreate(
            name="人员甲",
            position="组员",
            department_ids=[2],
            id_card_number="99999919990101999X",
            account_mode="existing",
            existing_user_id=10,
        )
        with self.assertRaises(HTTPException) as create_error:
            await create_member(
                create_payload,
                request=None,
                user={"role": "admin"},
                conn=None,
            )
        self.assertEqual(create_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as update_error:
            await update_member(
                1,
                GridMemberUpdate(id_card_number="99999919990101999X"),
                request=None,
                user={"role": "admin"},
                conn=None,
            )
        self.assertEqual(update_error.exception.status_code, 403)

    async def test_super_admin_can_replace_identity_without_audit_value(self):
        cursor = IdentityUpdateCursor()
        connection = FakeConnection(cursor)
        with (
            patch(
                "routers.grid_members.get_member_departments",
                AsyncMock(return_value={1: [{"id": 2, "community_name": "南厍"}]}),
            ),
            patch("routers.grid_members.record_admin_audit", AsyncMock()) as audit,
            patch("routers.grid_members.request_audit_fields", return_value={}),
        ):
            result = await update_member(
                1,
                GridMemberUpdate(id_card_number="99999919990101999X"),
                request=None,
                user={"id": 1, "role": "super_admin"},
                conn=connection,
            )

        self.assertEqual(result["message"], "修改成功")
        self.assertTrue(connection.committed)
        update_call = next(
            call for call in cursor.calls
            if call[0].startswith("UPDATE _grid_members SET id_card_number=%s")
        )
        self.assertEqual(update_call[1], ["99999919990101999X", 1])
        audit_detail = audit.await_args.kwargs["detail"]
        self.assertTrue(audit_detail["identity_changed"])
        self.assertNotIn("99999919990101999X", str(audit_detail))

    async def test_duplicate_identity_is_rejected(self):
        cursor = IdentityUpdateCursor(duplicate=True)
        connection = FakeConnection(cursor)
        with patch(
            "routers.grid_members.get_member_departments",
            AsyncMock(return_value={1: [{"id": 2, "community_name": "南厍"}]}),
        ):
            with self.assertRaises(HTTPException) as raised:
                await update_member(
                    1,
                    GridMemberUpdate(id_card_number="99999919990101999X"),
                    request=None,
                    user={"id": 1, "role": "super_admin"},
                    conn=connection,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(connection.rolled_back)

    async def test_linked_accounts_are_swapped_and_sessions_invalidated(self):
        cursor = ReassignmentCursor()
        result = await _reassign_member_account(
            cursor,
            member_id=1,
            target_account_id=20,
        )
        self.assertTrue(result["changed"])
        self.assertTrue(result["swapped"])
        self.assertEqual(result["affected_account_ids"], [10, 20])
        session_delete = next(
            call for call in cursor.calls
            if call[0].startswith("DELETE FROM _sessions")
        )
        self.assertEqual(session_delete[1], [10, 20])
        inherited_updates = [
            call for call in cursor.calls
            if call[0].startswith("UPDATE _users SET member_id=%s")
        ]
        self.assertEqual(len(inherited_updates), 2)

    async def test_super_admin_target_account_is_rejected(self):
        cursor = ReassignmentCursor(target_member_id=None, target_role="super_admin")
        with self.assertRaises(HTTPException) as raised:
            await _reassign_member_account(
                cursor,
                member_id=1,
                target_account_id=20,
            )
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
