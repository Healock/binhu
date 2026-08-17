from __future__ import annotations

import asyncio
import os
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import police_dispatch_publish_jobs as jobs


class _Cursor:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []
        self.last_query = ""

    async def execute(self, query, params=None):
        self.last_query = " ".join(str(query).split())
        self.queries.append((self.last_query, params))

    async def fetchall(self):
        if "SELECT id,batch_id FROM _police_dispatch_publish_runs" in self.last_query:
            return [(41, 7)]
        return []

    async def fetchone(self):
        return None


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return _CursorContext(self._cursor)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_public_publish_run_contains_only_safe_progress_fields():
    now = datetime(2026, 8, 17, 8, 0)
    result = jobs._public_run((
        9, 7, "running", "publishing", 397, 150, 140, 2, 5, 3,
        "", "", now, None, now, now,
    ))

    assert result == {
        "id": 9, "batch_id": 7, "status": "running", "phase": "publishing",
        "total_count": 397, "processed_count": 150, "success_count": 140,
        "conflict_count": 2, "reconciliation_count": 5, "retryable_count": 3,
        "error_code": "", "error_message": "", "started_at": "2026-08-17T08:00:00Z",
        "finished_at": None, "created_at": "2026-08-17T08:00:00Z",
        "updated_at": "2026-08-17T08:00:00Z",
    }
    assert not {"identity_number", "phone", "address"}.intersection(result)


def test_recovery_freezes_sending_items_and_only_retries_unsent_items():
    cursor = _Cursor()
    pool = _Pool(_Conn(cursor))
    with patch.object(jobs.db_manager, "get_pool", return_value=pool):
        recovered = asyncio.run(jobs.recover_interrupted_police_publish_runs())

    assert recovered == 1
    sql = "\n".join(query for query, _params in cursor.queries)
    assert "item.status='sending'" in sql
    assert "task.publish_status='needs_reconciliation'" in sql
    assert "item.status IN ('queued','checking')" in sql
    assert "task.publish_status='retryable'" in sql
    assert "status='failed',phase='finished'" in sql
    assert "WHERE batch.id=%s" in sql
