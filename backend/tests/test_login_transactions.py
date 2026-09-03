import os
import unittest
from unittest.mock import patch

from fastapi import Response
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.auth import LoginRequest, login


USER_ROW = (
    7, "tester", "hash", "member", "table", "three", "dock", None,
    "light", "测试人员", None, "table",
)


class DatabaseError(Exception):
    pass


class Cursor:
    def __init__(self, *, fail_session_delete=None):
        self.fail_session_delete = fail_session_delete
        self.result = None
        self.sql = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        if "FROM _users WHERE username" in sql:
            self.result = USER_ROW
        elif "FROM _users WHERE id=%s FOR UPDATE" in sql:
            self.result = (7, "member", "old-desktop", None, "old-desktop")
        elif "SELECT config_key" in sql:
            self.result = [("maintenance_enabled", "0"), ("timezone", "Asia/Shanghai")]
        elif "SELECT UTC_TIMESTAMP()" in sql:
            self.result = (None,)
        elif sql.startswith("DELETE FROM _sessions") and self.fail_session_delete:
            error = self.fail_session_delete
            self.fail_session_delete = None
            raise error
        else:
            self.result = None

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.result or []


class Connection:
    def __init__(self, cursor):
        self.value = cursor
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.value

    async def begin(self):
        self.begins += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class Pool:
    def __init__(self, connections):
        self.connections = list(connections)
        self.released = []

    async def acquire(self):
        return self.connections.pop(0)

    def release(self, connection):
        self.released.append(connection)


def request():
    return Request({
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"user-agent", b"test")],
    })


class LoginTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadlock_retries_whole_transaction_without_generic_invalidation(self):
        read = Connection(Cursor())
        first = Connection(Cursor(fail_session_delete=DatabaseError(1213, "deadlock")))
        second = Connection(Cursor())
        pool = Pool([read, first, second])
        with (
            patch("routers.auth.db_manager.get_pool", return_value=pool),
            patch("routers.auth.production_username_allowed", return_value=True),
            patch("routers.auth.bcrypt.checkpw", return_value=True),
            patch("routers.auth.create_session", side_effect=["new-one", "new-two"]),
            patch("routers.auth.asyncio.sleep") as sleep,
        ):
            result = await login(
                LoginRequest(username="tester", password="secret"),
                request(), Response(),
            )

        self.assertEqual(result["message"], "登录成功")
        self.assertEqual(first.rollbacks, 1)
        self.assertEqual(second.commits, 1)
        sleep.assert_awaited_once_with(0.05)
        combined_sql = "\n".join(sql for connection in (first, second)
                                 for sql, _ in connection.value.sql)
        self.assertNotIn("WHERE active_session_id=", combined_sql)
        self.assertEqual(len(pool.released), 3)

    async def test_deadlock_is_raised_after_three_attempts(self):
        read = Connection(Cursor())
        attempts = [
            Connection(Cursor(fail_session_delete=DatabaseError(1213, "deadlock")))
            for _ in range(3)
        ]
        pool = Pool([read, *attempts])
        with (
            patch("routers.auth.db_manager.get_pool", return_value=pool),
            patch("routers.auth.production_username_allowed", return_value=True),
            patch("routers.auth.bcrypt.checkpw", return_value=True),
            patch("routers.auth.create_session", side_effect=["one", "two", "three"]),
            patch("routers.auth.asyncio.sleep"),
        ):
            with self.assertRaises(DatabaseError) as raised:
                await login(
                    LoginRequest(username="tester", password="secret"),
                    request(), Response(),
                )

        self.assertEqual(raised.exception.args[0], 1213)
        self.assertEqual([item.rollbacks for item in attempts], [1, 1, 1])
        self.assertEqual(sum(item.commits for item in attempts), 0)


if __name__ == "__main__":
    unittest.main()
