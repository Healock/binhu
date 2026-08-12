from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.workflow_photo_sheet import run_photo_sheet_sync


@pytest.mark.asyncio
async def test_manual_sync_flushes_outbox_before_full_read():
    order: list[str] = []

    async def outbox_once():
        order.append("outbox")
        return {"processed": 1, "failed": 0}

    async def sync_once(*, full: bool, actor_user_id: int):
        order.append("full" if full else "incremental")
        return {"created_tickets": 0, "completed_tickets": 0}

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/workflow/photo-sheet/sync",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })
    with (
        patch(
            "routers.workflow_photo_sheet.process_outbox_once",
            new=AsyncMock(side_effect=outbox_once),
        ),
        patch(
            "routers.workflow_photo_sheet.sync_online_once",
            new=AsyncMock(side_effect=sync_once),
        ),
        patch(
            "routers.workflow_photo_sheet.record_admin_audit",
            new=AsyncMock(),
        ),
    ):
        result = await run_photo_sheet_sync(
            request=request,
            full=True,
            user={"id": 7, "username": "synthetic-admin"},
        )

    assert order == ["outbox", "full"]
    assert result["outbox"]["processed"] == 1
