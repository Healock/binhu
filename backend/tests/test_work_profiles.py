from __future__ import annotations

import os
from datetime import datetime

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from database import ensure_work_activity_schema
from deps import get_current_user
from main import app
from routers.profiles import get_profile
from services.work_activity import (
    actual_online_work_fields,
    contribution_summary,
    is_actual_online_work,
    work_profile_key,
)


def test_actual_work_fields_exclude_navigation_and_assignment_changes():
    assert is_actual_online_work(["核查结果"])
    assert is_actual_online_work(["核查人", "现住址"])
    assert actual_online_work_fields(["社区", "核查人"]) == []
    assert not is_actual_online_work(["社区", "核查人", "备注"])


def test_profile_key_follows_member_instead_of_linked_account():
    assert work_profile_key(17, 9) == "member:9"
    assert work_profile_key(17, None) == "user:17"
    assert work_profile_key(33, 9) == "member:9"


def test_contribution_summary_uses_business_timezone_and_streaks():
    summary = contribution_summary([
        ("online_task_update", 1, datetime(2026, 8, 1, 15, 59)),
        ("online_task_update", 2, datetime(2026, 8, 1, 16, 1)),
        ("police_dispatch_review", 3, datetime(2026, 8, 2, 16, 1)),
        ("work_log", 1, datetime(2026, 8, 5, 3, 0)),
    ], timezone_name="Asia/Shanghai")

    assert summary["total"] == 7
    assert summary["active_days"] == 4
    assert summary["longest_streak"] == 3
    assert summary["days"] == [
        {"date": "2026-08-01", "count": 1},
        {"date": "2026-08-02", "count": 2},
        {"date": "2026-08-03", "count": 3},
        {"date": "2026-08-05", "count": 1},
    ]


class SchemaCursor:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, sql, params=None):
        del params
        self.executed.append(" ".join(sql.split()))


@pytest.mark.asyncio
async def test_history_backfill_is_idempotent_and_stores_no_business_body():
    cursor = SchemaCursor()
    await ensure_work_activity_schema(cursor)
    await ensure_work_activity_schema(cursor)

    inserts = [sql for sql in cursor.executed if "INTO _work_activity_events" in sql]
    assert inserts
    assert all(sql.startswith("INSERT IGNORE") for sql in inserts)
    create_sql = next(sql for sql in cursor.executed if sql.startswith("CREATE TABLE"))
    assert "before_values" not in create_sql
    assert "after_values" not in create_sql
    assert "identity_number" not in create_sql
    assert "phone" not in create_sql


class ProfileCursor:
    def __init__(self):
        self.last_sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        del params
        self.last_sql = " ".join(sql.split())

    async def fetchone(self):
        if "config_key = 'timezone'" in self.last_sql:
            return ("Asia/Shanghai",)
        if "WHERE user.id=%s" in self.last_sql:
            return (
                7,
                "张三",
                "组员",
                "长板",
                "长板",
                datetime(2026, 1, 2, 3, 4, 5),
                12,
            )
        return None

    async def fetchall(self):
        if "FROM _work_activity_events" in self.last_sql:
            return [
                ("member:12", "online_task_update", 1, datetime(2026, 8, 5, 1, 0)),
            ]
        return []


class ProfileConnection:
    def __init__(self):
        self.cursor_instance = ProfileCursor()

    def cursor(self):
        return self.cursor_instance


@pytest.mark.asyncio
async def test_public_profile_response_omits_account_and_sensitive_fields():
    payload = await get_profile(7, year=2026, conn=ProfileConnection())

    assert payload["display_name"] == "张三"
    assert payload["departments"] == ["长板"]
    assert payload["contribution"]["total"] == 1
    serialized_keys = repr(payload)
    for forbidden in (
        "username",
        "identity",
        "phone",
        "permission",
        "session",
        "password",
    ):
        assert forbidden not in serialized_keys.lower()


def test_public_profile_routes_still_require_login():
    routes = [route for route in app.routes if route.path.startswith("/api/profiles")]
    assert routes
    for route in routes:
        assert any(
            dependency.call is get_current_user
            for dependency in route.dependant.dependencies
        )
