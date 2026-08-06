import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.exports import XlsxExportAuditRequest, record_xlsx_export
from services.permissions import ONLINE_SUMMARY_VIEW


def _request():
    return SimpleNamespace(
        headers={"user-agent": "test"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_online_xlsx_export_records_only_safe_summary_metadata():
    payload = XlsxExportAuditRequest(
        export_type="online_summary",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 6),
        summary_type="全链条",
        inspector_rows=18,
        community_rows=7,
    )
    audit = AsyncMock(return_value=11)
    with patch("routers.exports.record_admin_audit", audit):
        result = await record_xlsx_export(
            payload,
            _request(),
            {"id": 3, "username": "user", "permissions": [ONLINE_SUMMARY_VIEW]},
        )

    assert result == {"recorded": True}
    assert audit.await_args.args[1] == "online_summary.export"
    detail = audit.await_args.kwargs["detail"]
    assert detail == {
        "file_format": "XLSX",
        "summary_type": "全链条",
        "start_date": "2026-08-01",
        "end_date": "2026-08-06",
        "inspector_rows": 18,
        "community_rows": 7,
    }


@pytest.mark.asyncio
async def test_xlsx_export_rejects_missing_concrete_permission():
    payload = XlsxExportAuditRequest(
        export_type="online_summary",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 6),
        summary_type="全链条",
        inspector_rows=0,
        community_rows=0,
    )
    with pytest.raises(HTTPException) as error:
        await record_xlsx_export(payload, _request(), {"permissions": []})
    assert error.value.status_code == 403
