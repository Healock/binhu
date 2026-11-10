"""Exercise migration pagination with a cursor whose writes replace results."""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")

from migrations import task_read_model_backfill as backfill
from migrations import task_view_state_revisions as revisions
from services import task_view_state


class Cursor:
    def __init__(self, count):
        self.rows = [
            {"id": i, "physical_row": i, "revision": 1,
             "values_json": {"result": "synthetic"}}
            for i in range(1, count + 1)
        ]
        self.result = []
        self.page_sizes = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, args=()):
        self.result = []
        if sql.startswith("SELECT id,physical_row"):
            assert "AND id>%s ORDER BY id LIMIT %s" in sql
            last, limit = args[-2:]
            self.result = [row for row in self.rows if row["id"] > last][:limit]
            self.page_sizes.append(len(self.result))

    async def fetchall(self):
        rows, self.result = self.result, []
        return rows

    async def fetchone(self):
        return self.result.pop(0) if self.result else None


class Connection:
    def __init__(self, cursor):
        self.current_cursor = cursor
        self.closed = False
        self.rollbacks = 0

    def cursor(self, *args):
        return self.current_cursor

    async def commit(self):
        pass

    async def rollback(self):
        self.rollbacks += 1

    def close(self):
        # Real aiomysql Connection has no wait_closed method.
        self.closed = True


@pytest.mark.asyncio
async def test_backfill_emits_all_pages_despite_interleaved_cursor_writes(monkeypatch):
    cursor = Cursor(501)
    conn = Connection(cursor)
    commands, events, states = [], [], []

    async def measure():
        return {"total": 501}

    async def connect(**kwargs):
        return conn

    async def append(cur, **kwargs):
        await cur.execute("INSERT command")
        commands.append(kwargs["source_id"])
        return {"encrypted": True}

    async def enqueue(cur, **kwargs):
        await cur.execute("INSERT outbox")
        events.append(kwargs["source_id"])

    async def view_state(cur, **kwargs):
        await cur.execute("INSERT view state")
        states.append(kwargs["source_id"])

    monkeypatch.setattr(backfill, "identity", lambda: ("development", "dev-synthetic"))
    monkeypatch.setattr(backfill, "measure", measure)
    monkeypatch.setattr(backfill, "_connect", connect)
    monkeypatch.setattr(backfill, "PARSER_REGISTRY", {
        "synthetic": SimpleNamespace(table_name="t_fullchain", COLUMNS=["result"]),
    })
    monkeypatch.setattr(backfill, "task_changed_field_categories", lambda *args: ["result"])
    monkeypatch.setattr(backfill, "append_task_command", append)
    monkeypatch.setattr(backfill, "enqueue_event", enqueue)
    monkeypatch.setattr(task_view_state, "enqueue_current_task_view_state_snapshot", view_state)

    result = await backfill.apply()

    assert result["emitted"] == 501
    assert commands == events == states == list(range(1, 502))
    assert cursor.page_sizes == [250, 250, 1, 0]
    assert conn.closed
    assert conn.rollbacks == 0


@pytest.mark.asyncio
async def test_revision_measure_closes_real_driver_shaped_connection(monkeypatch):
    conn = Connection(Cursor(0))

    async def connect(**kwargs):
        return conn

    async def inspect(cur):
        return {"missing_columns": [], "primary_key_present": True}

    monkeypatch.setattr(revisions.settings, "APP_ENVIRONMENT", "development")
    monkeypatch.setattr(revisions.aiomysql, "connect", connect)
    monkeypatch.setattr(revisions, "inspect", inspect)

    result = await revisions.run("measure")

    assert result["consistent"] is True
    assert conn.closed
