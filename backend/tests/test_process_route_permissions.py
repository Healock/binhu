import os
from datetime import datetime

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.processes import router


def test_process_starters_can_read_the_definition_needed_by_the_start_form():
    route = next(
        item for item in router.routes
        if item.path == "/api/processes/definitions/{process_definition_id}"
        and "GET" in item.methods
    )
    user_dependency = next(
        dependency.call for dependency in route.dependant.dependencies
        if dependency.name == "user"
    )

    assert user_dependency.__name__ == "require_process_definition_access"


@pytest.mark.asyncio
async def test_definition_read_allows_starters_and_process_config_admins_only():
    from fastapi import HTTPException

    from routers.processes import require_process_definition_access

    assert await require_process_definition_access(
        user={"permissions": ["workflow.ticket.create"]}
    )
    assert await require_process_definition_access(
        user={"permissions": ["workflow.config.manage"]}
    )
    with pytest.raises(HTTPException) as error:
        await require_process_definition_access(user={"permissions": []})
    assert error.value.status_code == 403


def test_starting_a_process_still_requires_create_permission():
    route = next(
        item for item in router.routes
        if item.path == "/api/processes/instances" and "POST" in item.methods
    )
    user_dependency = next(
        dependency.call for dependency in route.dependant.dependencies
        if dependency.name == "user"
    )

    assert user_dependency.__name__ == "require_workflow_ticket_create"


def test_process_start_form_only_selects_the_current_published_version():
    import inspect

    from routers.processes import get_process_definition

    source = inspect.getsource(get_process_definition)
    assert "if not is_config_admin and (not row[7] or not row[8])" in source
    assert "WHERE id=%s AND process_definition_id=%s AND status='published'" in source
    assert "form_version.status='published'" in source


class _FakeCursor:
    def __init__(self):
        self.statements = []
        self.fetches = [
            (7, "sample", "Sample", "", 3, "workflow", 24, 1, 12,
             '{"fields":[{"key":"reason","type":"textarea"}]}'),
            (12, 1, "[]", "published", 5, datetime(2026, 1, 1), datetime(2026, 1, 1)),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement, params):
        self.statements.append((statement, params))

    async def fetchone(self):
        return self.fetches.pop(0)

    async def fetchall(self):
        return [
            (12, 1, "[]", "published", 5, datetime(2026, 1, 1), datetime(2026, 1, 1)),
            (11, 0, "[]", "draft", 5, None, datetime(2025, 12, 1)),
        ]


class _FakeConnection:
    def __init__(self):
        self.fake_cursor = _FakeCursor()

    def cursor(self):
        return self.fake_cursor


@pytest.mark.asyncio
async def test_start_form_receives_current_published_schema_and_version():
    from routers.processes import get_process_definition

    conn = _FakeConnection()
    result = await get_process_definition(7, conn=conn, user={"id": 5})

    assert result["current_version_id"] == 12
    assert result["form_schema"] == {"fields": [{"key": "reason", "type": "textarea"}]}
    assert result["versions"][0]["id"] == 12
    assert len(conn.fake_cursor.statements) == 2


@pytest.mark.asyncio
async def test_process_config_admin_keeps_access_to_all_versions():
    from routers.processes import get_process_definition

    conn = _FakeConnection()
    result = await get_process_definition(
        7, conn=conn, user={"id": 5, "permissions": ["workflow.config.manage"]}
    )

    assert [item["id"] for item in result["versions"]] == [12, 11]
    assert "ORDER BY version_no DESC" in conn.fake_cursor.statements[1][0]
