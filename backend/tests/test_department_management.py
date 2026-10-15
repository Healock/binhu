from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import grid_members
from routers.grid_members import (
    DepartmentCreate,
    DepartmentStatusUpdate,
    _validate_department_parent,
    _validate_department_payload,
    delete_department,
    list_communities,
    update_department_status,
)


class CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, *_args):
        return False


class FakeCursor:
    def __init__(self, one=(), all_rows=()):
        self.one = list(one)
        self.all_rows = list(all_rows)
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.one.pop(0) if self.one else None

    async def fetchall(self):
        return list(self.all_rows)


def fake_connection(cursor):
    conn = MagicMock()
    conn.begin = AsyncMock()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    conn.cursor = MagicMock(return_value=CursorContext(cursor))
    return conn


def request():
    return Request({
        "type": "http", "method": "PATCH", "path": "/", "headers": [],
        "client": ("127.0.0.1", 1),
    })


@pytest.mark.asyncio
async def test_unit_department_requires_null_community_and_existing_area():
    data = DepartmentCreate(name="社区警务队", department_type="unit")
    await _validate_department_payload(FakeCursor(), data)

    invalid = DepartmentCreate(
        name="单位", department_type="unit", community_id=3
    )
    with pytest.raises(HTTPException, match="单位级部门不能关联社区"):
        await _validate_department_payload(FakeCursor(), invalid)

    with pytest.raises(HTTPException, match="该片区已关联其他单位部门"):
        await _validate_department_payload(
            FakeCursor(one=[(5,), (8,)]),
            DepartmentCreate(name="中片", department_type="unit", area_id=4),
        )


@pytest.mark.asyncio
async def test_parent_must_be_active_unit_and_cycle_check_is_recursive():
    with pytest.raises(HTTPException, match="上级部门必须是单位级部门"):
        await _validate_department_parent(
            FakeCursor(one=[(9, "community", 1)]), department_id=4, parent_id=9
        )

    cursor = FakeCursor(one=[(9, "unit", 1), (1,)])
    with pytest.raises(HTTPException, match="自己的下级"):
        await _validate_department_parent(cursor, department_id=4, parent_id=9)
    recursive = next(sql for sql, _ in cursor.calls if "WITH RECURSIVE" in sql)
    assert "UNION DISTINCT" in recursive


@pytest.mark.asyncio
async def test_department_with_members_cannot_be_disabled_and_returns_recovery_path():
    cursor = FakeCursor(one=[("片区", 1), (2,), (0,)])
    conn = fake_connection(cursor)
    with pytest.raises(HTTPException) as raised:
        await update_department_status(
            4, DepartmentStatusUpdate(is_active=False), request(),
            {"id": 1}, conn,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["member_count"] == 2
    assert raised.value.detail["next_step"]
    assert raised.value.detail["allowed_role"]
    assert raised.value.detail["recovery"]
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_community_department_cannot_be_deleted_from_department_page():
    cursor = FakeCursor(one=[("冬梅", "community", 3)])
    conn = fake_connection(cursor)
    with pytest.raises(HTTPException) as raised:
        await delete_department(8, request(), {"id": 1}, conn)

    assert raised.value.status_code == 409
    assert raised.value.detail["next_step"]
    assert len(cursor.calls) == 1


@pytest.mark.asyncio
async def test_community_member_count_query_remains_scoped_to_community_departments(monkeypatch):
    cursor = FakeCursor()
    conn = fake_connection(cursor)
    await list_communities({}, conn)

    summary_sql = cursor.calls[0][0]
    assert "department.department_type='community'" in summary_sql
    assert "GROUP BY department.community_id" in summary_sql
    assert "department_type='unit'" not in summary_sql


@pytest.mark.asyncio
async def test_no_module_access_gate_was_added_to_department_routes():
    paths = {route.path for route in grid_members.router.routes}
    assert "/api/grid-members/departments" in paths
    assert "/api/grid-members/departments/{department_id}" in paths
    assert not any("module" in path for path in paths)
