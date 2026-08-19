from __future__ import annotations

import os
from datetime import date, datetime

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.visit_sources import _run_view


def test_pending_preview_can_be_recovered_from_saved_safe_summary():
    row = (
        48, "detail", "manual", "pending_confirmation", 1,
        date(2026, 6, 11), date(2026, 7, 1), date(2026, 6, 11),
        "走访明细", 3611, 3611, 0,
        '{"issues":[],"diff":{"inserted":5,"updated":1,"deleted":0}}',
        None, None, None, None, datetime(2026, 8, 19, 7, 7, 19),
    )

    result = _run_view(row)

    assert result["issues"] == []
    assert result["diff"] == {"inserted": 5, "updated": 1, "deleted": 0}
    assert "payload_json" not in result
