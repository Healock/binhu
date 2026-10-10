from __future__ import annotations

import json
import os
from io import BytesIO
from unittest.mock import AsyncMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook
from routers import registry, registry_extended as extended
from services.registry_import import normalize_household_status


def workbook(status="已注销", header="注销状态"):
    book = Workbook()
    book.active.append(["出租屋地址", "住房类型", "社区名称", *([header] if header else [])])
    book.active.append(["合成路1号", "个人出租", "合成社区", *([status] if header else [])])
    result = BytesIO()
    book.save(result)
    return result.getvalue()


@pytest.mark.parametrize("header", ["注销状态", "房屋状态", "房屋登记状态", "状态"])
def test_parser_preserves_household_source_status(header):
    rows = extended._parse_household_workbook(workbook(header=header))
    assert rows[0]["household_status"] == "已注销"


def test_explicit_cancellation_header_wins_over_generic_status():
    book = Workbook()
    book.active.append(["出租屋地址", "住房类型", "状态", "注销状态"])
    book.active.append(["合成路1号", "个人出租", "未注销", "已注销"])
    result = BytesIO()
    book.save(result)
    assert extended._parse_household_workbook(result.getvalue())[0]["household_status"] == "已注销"


def test_missing_status_is_distinct_from_explicit_blank_and_unknown_codes():
    assert "household_status" not in extended._parse_household_workbook(workbook(header=None))[0]
    assert extended._parse_household_workbook(workbook(status=""))[0]["household_status"] == ""
    for value in ["", None, "0", "1", "不认识的状态"]:
        assert normalize_household_status(value) == ""
    assert normalize_household_status("已注销") == "已注销"
    assert normalize_household_status("未注销") == "未注销"
    assert normalize_household_status("正常") == "未注销"


class Connection:
    def __init__(self, payload=None, existing=True):
        self.calls = []
        self.sql = ""
        self.payload = payload or {"address": "合成路1号", "community": "合成社区", "housing_type": "个人出租"}
        self.existing = existing
        self.committed = False
        self.rolled_back = False
        self.batch_status = "imported"

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, sql, params=()):
        self.sql = sql
        self.calls.append((sql, params))

    async def executemany(self, sql, rows):
        await self.execute(sql, list(rows))

    async def fetchone(self):
        if self.sql.startswith("SELECT status FROM"):
            return ("preview",)
        if self.sql.startswith("SELECT id, status FROM"):
            return (1, self.batch_status)
        if "JSON_CONTAINS_PATH" in self.sql:
            return None
        if "SELECT COUNT" in self.sql:
            return (0,)
        return (1,)

    async def fetchall(self):
        if self.sql.startswith("SELECT id, source_ref, payload_json"):
            return [(1, "Sheet:2", json.dumps(self.payload))]
        if self.sql.startswith("SELECT id, community_id, street"):
            return [(42, 8, "", "合成路1号", "", "", "合成路1号")] if self.existing else []
        if self.sql.startswith("SELECT id, community_id, normalized_address"):
            return [(42, 8, "合成路1号")]
        return []

    async def begin(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("state,expected", [("已注销", "已注销"), ("未注销", "未注销"), ("1", ""), (None, None)])
async def test_confirm_persists_status_without_repurposing_platform_state(monkeypatch, existing, state, expected):
    conn = Connection(existing=existing)
    if state is not None:
        conn.payload["household_status"] = state
    monkeypatch.setattr(extended, "_household_import_community", AsyncMock(return_value=(8, "合成社区")))
    monkeypatch.setattr(extended, "record_admin_audit", AsyncMock())
    monkeypatch.setattr(extended, "request_audit_fields", lambda *_: {})
    result = await extended.confirm_household_import(1, None, {"id": 7}, conn)
    assert conn.committed and result["imported_count"] == 1
    prefix = "UPDATE registry_properties SET" if existing else "INSERT INTO registry_properties"
    statement, values = next((sql, params) for sql, params in conn.calls if sql.startswith(prefix))
    assert "household_status" in statement
    assert "SET status=" not in statement
    if existing:
        assert values[-5:-2] == (expected, expected, expected)
        assert "current_version=current_version+IF" in statement
    else:
        assert values[0][-3] == (expected or "")
        assert statement.count("%s") == len(values[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_status", ["preview", "imported", "partially_imported"])
async def test_old_identical_file_cannot_silently_skip_status_upgrade(batch_status):
    conn = Connection()
    conn.batch_status = batch_status
    with pytest.raises(HTTPException) as error:
        await extended.preview_household_import(None, UploadFile(filename="synthetic.xlsx", file=BytesIO(workbook())), {"id": 7}, conn)
    assert error.value.status_code == 409
    assert "另存" in error.value.detail
    assert conn.rolled_back and not conn.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("state,value", [("cancelled", "已注销"), ("not_cancelled", "未注销"), ("unknown", None), ("", None)])
async def test_search_and_export_share_cancellation_filter(monkeypatch, state, value):
    from tests.test_registry_foundation import _PropertySearchConnection
    monkeypatch.setattr(registry, "_allowed_community_ids", AsyncMock(return_value=[8]))
    for export_all in [False, True]:
        conn = _PropertySearchConnection()
        await registry._property_search_result(registry.PropertySearch(household_status=state), {"id": 7}, conn, export_all=export_all)
        sql, params = conn.search_cursor.calls[0]
        assert "property.status=%s" not in sql
        assert "community_id IN" in sql
        if value:
            assert "property.household_status=%s" in sql and value in params
        elif state == "unknown":
            assert "COALESCE(property.household_status,'')=''" in sql
        else:
            assert "property.household_status" not in sql
