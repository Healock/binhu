from __future__ import annotations

import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import watch_matching


class _PayloadCursor:
    def __init__(self, snapshot_rows, current_rows):
        self.snapshot_rows = snapshot_rows
        self.current_rows = current_rows
        self.calls = []
        self._fetch_count = 0

    async def execute(self, sql, params=()):
        self.calls.append((str(sql), tuple(params)))

    async def fetchall(self):
        self._fetch_count += 1
        return self.snapshot_rows if self._fetch_count == 1 else self.current_rows


@pytest.mark.asyncio
async def test_model_three_payload_falls_back_to_current_registry_tag(monkeypatch):
    monkeypatch.setattr(watch_matching.settings, "REGISTRY_FEATURE_ENABLED", True)
    monkeypatch.setattr(watch_matching.settings, "MYSQL_REGISTRY_DB", "RegistryData")
    cursor = _PayloadCursor(
        snapshot_rows=[],
        current_rows=[
            (
                "row-1", None, 7, "自购自住", "#16a34a", "notice",
                "active", "asset_import", "active", "current_registry_tag",
            ),
        ],
    )

    result = await watch_matching.task_watch_payload(
        cursor, "疑似未注销模型三", ["row-1"],
    )

    assert result["row-1"]["watch_marks"] == [{
        "category_id": 7,
        "name": "自购自住",
        "color": "#16a34a",
        "alert_level": "notice",
        "assignment_status": "active",
        "source_type": "asset_import",
        "snapshot_status": "active",
        "snapshot_reason": "current_registry_tag",
    }]
    assert len(cursor.calls) == 2


@pytest.mark.asyncio
async def test_model_three_payload_does_not_duplicate_existing_snapshot(monkeypatch):
    monkeypatch.setattr(watch_matching.settings, "REGISTRY_FEATURE_ENABLED", True)
    monkeypatch.setattr(watch_matching.settings, "MYSQL_REGISTRY_DB", "RegistryData")
    cursor = _PayloadCursor(
        snapshot_rows=[
            ("row-1", None, 7, "自购自住", "#16a34a", "notice", "active", "asset_import", "active", "initial_match"),
        ],
        current_rows=[],
    )

    result = await watch_matching.task_watch_payload(
        cursor, "疑似未注销模型三", ["row-1"],
    )

    assert len(result["row-1"]["watch_marks"]) == 1
    assert result["row-1"]["watch_marks"][0]["snapshot_reason"] == "initial_match"
