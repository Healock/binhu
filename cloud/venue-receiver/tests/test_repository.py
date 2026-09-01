import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repository import MySQLRepository


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self.rowcount = 1 if sql.lstrip().upper().startswith("UPDATE SUBMISSIONS") else 0

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def cursor(self, *_args):
        return self._cursor

    async def begin(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, cursor):
        self.connection = FakeConnection(cursor)

    def acquire(self):
        return FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_acknowledge_applies_terminal_states_and_delays_retry_later():
    cursor = FakeCursor()
    pool = FakePool(cursor)
    repository = MySQLRepository(pool, queued_retention_hours=168, lease_seconds=300)

    applied = await repository.acknowledge(
        "lease-id",
        [
            {"submission_id": "accepted-id", "status": "accepted", "reason_code": ""},
            {"submission_id": "rejected-id", "status": "rejected", "reason_code": "payload_invalid"},
            {"submission_id": "retry-id", "status": "retry_later", "reason_code": "storage_unavailable"},
            {"submission_id": "uncertain-id", "status": "uncertain", "reason_code": "commit_unknown"},
        ],
    )

    assert applied == [
        {"submission_id": "accepted-id", "status": "accepted"},
        {"submission_id": "rejected-id", "status": "rejected"},
        {"submission_id": "retry-id", "status": "retry_later"},
        {"submission_id": "uncertain-id", "status": "uncertain"},
    ]
    retry_sql = next(sql for sql, params in cursor.executed if params and params[1] == "retry-id")
    assert "state='leased'" in retry_sql
    assert "lease_id=NULL" in retry_sql
    assert "INTERVAL 1 MINUTE" in retry_sql
    terminal_sql = [sql for sql, params in cursor.executed if params and params[1] != "retry-id"]
    assert all("acknowledged_at=UTC_TIMESTAMP()" in sql for sql in terminal_sql)
    assert pool.connection.commits == 1
    assert pool.connection.rollbacks == 0


@pytest.mark.asyncio
async def test_available_count_includes_only_queued_or_expired_leases():
    cursor = FakeCursor(rows=[(3,)])
    repository = MySQLRepository(FakePool(cursor), queued_retention_hours=168, lease_seconds=300)

    assert await repository.available_submission_count() == 3
    sql, _params = cursor.executed[0]
    assert "state='queued'" in sql
    assert "state='leased' AND lease_expires_at<UTC_TIMESTAMP()" in sql
    assert "expires_at>=UTC_TIMESTAMP()" in sql


@pytest.mark.asyncio
async def test_renew_lease_is_bound_to_owner_and_active_lease():
    cursor = FakeCursor()
    pool = FakePool(cursor)
    repository = MySQLRepository(pool, queued_retention_hours=168, lease_seconds=300)

    expires_at = await repository.renew_lease("lease-id", "binhu-primary")

    sql, params = cursor.executed[0]
    assert "lease_id=%s AND lease_owner=%s AND state='leased'" in sql
    assert params[1:] == ("lease-id", "binhu-primary")
    assert expires_at == params[0]
    assert pool.connection.commits == 1
