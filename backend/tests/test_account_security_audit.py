import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Response
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.auth import (
    ChangePasswordRequest,
    change_password,
    revoke_all_sessions,
    revoke_other_sessions,
    revoke_session,
)
from routers.users import UpdateUserRequest, update_user
from services.audit_display import ACTION_LABELS


def request(*, session_id: str = "session-current") -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [
            (b"cookie", f"binhu_session={session_id}".encode()),
            (b"user-agent", b"security-audit-test"),
            (b"x-forwarded-for", b"127.0.0.1"),
        ],
        "client": ("127.0.0.1", 12345),
    })


class SecurityCursor:
    def __init__(self):
        self.last_sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        del params
        self.last_sql = " ".join(sql.split())

    async def fetchone(self):
        if self.last_sql.startswith("SELECT password_hash FROM _users"):
            return ("stored-password-hash",)
        if self.last_sql.startswith("SELECT session_id FROM _sessions"):
            return ("session-other",)
        return None

    async def fetchall(self):
        if self.last_sql.startswith("SELECT session_id FROM _sessions"):
            return [("session-other",), ("session-third",)]
        return []


class UserUpdateCursor(SecurityCursor):
    async def fetchone(self):
        if "WHERE user.id=%s FOR UPDATE" in self.last_sql:
            return (None, "inherited", 2, "admin")
        return await super().fetchone()

    async def fetchall(self):
        if self.last_sql.startswith(
            "SELECT permission_group_id FROM _user_permission_group_links"
        ):
            return [(2,)]
        return await super().fetchall()


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    async def begin(self):
        return None

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class FakePool:
    def __init__(self, cursor):
        self.connection = FakeConnection(cursor)

    async def acquire(self):
        return self.connection

    def release(self, _connection):
        return None


class AccountSecurityAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_change_records_safe_audit_event(self):
        audit = AsyncMock()
        invalidate = AsyncMock()
        with (
            patch(
                "routers.auth.db_manager.get_pool",
                return_value=FakePool(SecurityCursor()),
            ),
            patch("routers.auth.bcrypt.checkpw", return_value=True),
            patch("routers.auth.bcrypt.hashpw", return_value=b"new-password-hash"),
            patch("routers.auth.invalidate_all_sessions", invalidate),
            patch("routers.auth.record_admin_audit", audit),
        ):
            await change_password(
                ChangePasswordRequest(
                    current_password="current-password-secret",
                    new_password="new-password-secret",
                ),
                request(),
                user={"id": 7, "username": "tester"},
            )

        invalidate.assert_awaited_once()
        audit.assert_awaited_once()
        call = audit.await_args
        self.assertEqual(call.args[1], "account.password.change")
        audit_text = repr(call)
        self.assertNotIn("current-password-secret", audit_text)
        self.assertNotIn("new-password-secret", audit_text)
        self.assertNotIn("new-password-hash", audit_text)

    async def test_admin_password_reset_has_distinct_safe_audit_event(self):
        audit = AsyncMock()
        groups = [{"id": 2, "code": "admin", "name": "管理员"}]
        with (
            patch(
                "routers.users.db_manager.get_pool",
                return_value=FakePool(UserUpdateCursor()),
            ),
            patch("routers.users._ensure_member_available", AsyncMock()),
            patch("routers.users._resolve_groups", AsyncMock(return_value=groups)),
            patch("routers.users._replace_custom_group_links", AsyncMock()),
            patch("routers.users.invalidate_all_sessions", AsyncMock()) as invalidate,
            patch("routers.users.bcrypt.hashpw", return_value=b"reset-password-hash"),
            patch("routers.users.record_admin_audit", audit),
        ):
            await update_user(
                23,
                UpdateUserRequest(
                    password="reset-password-secret",
                    password_is_temporary=True,
                ),
                request(),
                user={"id": 1, "username": "root"},
            )

        invalidate.assert_awaited_once()
        audit.assert_awaited_once()
        call = audit.await_args
        self.assertEqual(call.args[1], "user.password.reset")
        self.assertEqual(
            ACTION_LABELS["user.password.reset"],
            "管理员重置用户密码",
        )
        self.assertEqual(call.kwargs["target_name"], "23")
        self.assertEqual(call.kwargs["detail"]["temporary_password"], True)
        self.assertEqual(call.kwargs["detail"]["sessions_invalidated"], True)
        audit_text = repr(call)
        self.assertNotIn("reset-password-secret", audit_text)
        self.assertNotIn("reset-password-hash", audit_text)

    async def test_single_session_revocation_is_audited(self):
        audit = AsyncMock()
        with (
            patch(
                "routers.auth.db_manager.get_pool",
                return_value=FakePool(SecurityCursor()),
            ),
            patch("routers.auth.invalidate_session", AsyncMock()) as invalidate,
            patch("routers.auth.record_admin_audit", audit),
        ):
            await revoke_session(
                "device-management-id",
                request(),
                user={"id": 7, "username": "tester"},
            )

        invalidate.assert_awaited_once_with(unittest.mock.ANY, "session-other")
        self.assertEqual(audit.await_args.args[1], "account.session.revoke")
        self.assertEqual(audit.await_args.kwargs["detail"], {"revoked_sessions": 1})

    async def test_other_session_revocation_is_audited_with_count(self):
        audit = AsyncMock()
        with (
            patch(
                "routers.auth.db_manager.get_pool",
                return_value=FakePool(SecurityCursor()),
            ),
            patch("routers.auth.invalidate_session", AsyncMock()),
            patch("routers.auth.record_admin_audit", audit),
        ):
            result = await revoke_other_sessions(
                request(),
                user={"id": 7, "username": "tester"},
            )

        self.assertEqual(result["revoked"], 2)
        self.assertEqual(audit.await_args.args[1], "account.session.revoke_others")
        self.assertEqual(audit.await_args.kwargs["detail"], {"revoked_sessions": 2})

    async def test_all_session_revocation_is_audited(self):
        audit = AsyncMock()
        with (
            patch(
                "routers.auth.db_manager.get_pool",
                return_value=FakePool(SecurityCursor()),
            ),
            patch("routers.auth.invalidate_all_sessions", AsyncMock()) as invalidate,
            patch("routers.auth.record_admin_audit", audit),
        ):
            await revoke_all_sessions(
                request(),
                Response(),
                user={"id": 7, "username": "tester"},
            )

        invalidate.assert_awaited_once()
        self.assertEqual(audit.await_args.args[1], "account.session.revoke_all")
        self.assertEqual(
            audit.await_args.kwargs["detail"],
            {"includes_current_session": True},
        )


if __name__ == "__main__":
    unittest.main()
