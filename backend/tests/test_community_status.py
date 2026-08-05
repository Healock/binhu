from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.grid_members import CommunityStatusUpdate, update_community_status


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, *_args):
        return False


def test_disable_community_reports_member_and_pending_task_blockers():
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(side_effect=[
        ("长板", 1),
        (2,),
        (3,),
    ])
    conn = MagicMock()
    conn.begin = AsyncMock()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    conn.cursor = MagicMock(return_value=_CursorContext(cursor))
    request = Request({
        "type": "http", "method": "PATCH", "path": "/", "headers": [],
        "client": ("127.0.0.1", 1),
    })

    with pytest.raises(HTTPException) as error:
        asyncio.run(update_community_status(
            community_id=7,
            data=CommunityStatusUpdate(is_active=False),
            request=request,
            user={"id": 1, "username": "admin"},
            conn=conn,
        ))

    assert error.value.status_code == 409
    assert error.value.detail["member_count"] == 2
    assert error.value.detail["pending_task_count"] == 3
    conn.rollback.assert_awaited_once()
