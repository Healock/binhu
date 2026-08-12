from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow_extended import (
    require_attachment_manage_access,
    router,
)
from services.permissions import (
    WORKFLOW_ATTACHMENT_VIEW,
    WORKFLOW_TICKET_HANDLE,
    WORKFLOW_TICKET_MANAGE,
)


def _route(path: str, method: str):
    return next(
        route
        for route in router.routes
        if route.path == path and method in route.methods
    )


def _dependency_names(path: str, method: str) -> set[str]:
    return {
        dependency.call.__name__
        for dependency in _route(path, method).dependant.dependencies
    }


@pytest.mark.asyncio
async def test_attachment_view_permission_cannot_manage_files():
    user = {"permissions": [WORKFLOW_ATTACHMENT_VIEW]}
    with pytest.raises(HTTPException) as exc_info:
        await require_attachment_manage_access(user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [WORKFLOW_TICKET_HANDLE, WORKFLOW_TICKET_MANAGE])
async def test_ticket_handlers_and_managers_can_manage_files(permission: str):
    user = {"permissions": [permission]}
    assert await require_attachment_manage_access(user) is user


def test_attachment_routes_separate_view_and_manage_permissions():
    list_dependencies = _dependency_names(
        "/api/workflow/tickets/{ticket_id}/attachments", "GET"
    )
    upload_dependencies = _dependency_names(
        "/api/workflow/tickets/{ticket_id}/attachments", "POST"
    )
    download_dependencies = _dependency_names(
        "/api/workflow/tickets/{ticket_id}/attachments/{file_id}", "GET"
    )
    delete_dependencies = _dependency_names(
        "/api/workflow/tickets/{ticket_id}/attachments/{file_id}", "DELETE"
    )

    assert "require_workflow_attachment_view" in list_dependencies
    assert "require_workflow_attachment_view" in download_dependencies
    assert "require_attachment_manage_access" in upload_dependencies
    assert "require_attachment_manage_access" in delete_dependencies
