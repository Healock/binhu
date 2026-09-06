"""Independent, fixture-scoped raw inputs must never depend on projections."""
import hashlib
import json
import os
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from database import get_db
from routers import derived_inputs


class Cursor:
    def __init__(self, row):
        self.row = row
        self.execute = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def fetchone(self):
        return self.row


def make_row(**changes):
    values = {"现住址": "压测小区01压测楼01幢01室", "社区": "压测社区",
              "核查结果": "", "核查人": "FICTIONAL-INSPECTOR",
              "身份证号": "FICTIONAL-IDENTITY", "手机号": "FICTIONAL-PHONE",
              "备注": "PRIVATE-BODY"}
    values.update(changes.pop("values", {}))
    digest = hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    data = dict(source_id=9, parser="全链条", revision=8, source_hash=digest,
                values=json.dumps(values, ensure_ascii=False), local_revision=8,
                local_hash=digest, kind="local_table", ref="t_fullchain:27",
                updated_at=datetime(2026, 9, 7), fixture_run="KSHADOW-test01")
    data.update(changes)
    return tuple(data.values())


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "KSHADOW-test01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "raw-fixture-secret")


async def request(row=None, *, fields=None, headers=None, revision=8, task_id="t_fullchain:27"):
    calls = []
    cursor = Cursor(row)
    class Connection:
        def cursor(self):
            return cursor
    async def db():
        calls.append(True)
        yield Connection()
    app = FastAPI()
    app.include_router(derived_inputs.router)
    app.dependency_overrides[get_db] = db
    params = {"source_id": 9, "revision": revision}
    if fields is not None:
        params["fields"] = fields
    default_headers = {"X-Binhu-Internal-Token": "raw-fixture-secret",
                       "X-Binhu-Environment": "shadow", "X-Binhu-Run-Id": "KSHADOW-test01"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(f"/internal/v1/derived-input/raw/tasks/{task_id}",
                                  params=params, headers=default_headers if headers is None else headers)
    return result, calls, cursor


@pytest.mark.anyio
async def test_raw_inputs_are_source_only_and_fixture_scoped(setup):
    response, _, cursor = await request(make_row())
    assert response.status_code == 200
    body = response.json()
    assert body["fields"]["address"] == "压测小区01压测楼01幢01室"
    assert body["fields"]["community"] == "压测社区"
    assert body["fields"]["task_type"] == "全链条"
    assert body["fields"]["inspector_key"] and body["fields"]["inspector_key"] != "FICTIONAL-INSPECTOR"
    assert all(value not in response.text for value in (
        "FICTIONAL-INSPECTOR", "FICTIONAL-IDENTITY", "FICTIONAL-PHONE", "PRIVATE-BODY"))
    assert body["revision"] == 8 and body["source_id"] == 9 and body["task_id"] == "t_fullchain:27"
    assert body["reference_versions"] == {}
    assert set(body["unavailable_references"]) == {"address_catalog", "person_tags", "task_dependencies", "daily_report_history"}
    actual_hash = body.pop("readback_hash")
    assert actual_hash == derived_inputs._hash_payload(body)
    sql, params = cursor.execute.await_args.args
    assert "_shadow_business_expectations" in sql and "KSHADOW-test01" in params
    assert "_online_source_projection" not in sql and "_online_task_address_matches" not in sql


@pytest.mark.anyio
@pytest.mark.parametrize("fields", ["task_state", "address,手机号", "standard_address", "inspector", "values_json"])
async def test_raw_unknown_field_rejected_before_database(setup, fields):
    response, calls, _ = await request(make_row(), fields=fields)
    assert response.status_code == 422 and calls == []


@pytest.mark.anyio
async def test_raw_auth_and_kshadow_scope_before_database(setup, monkeypatch):
    response, calls, _ = await request(make_row(), headers={})
    assert response.status_code == 401 and calls == []
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-old01")
    response, calls, _ = await request(make_row(), headers={
        "X-Binhu-Internal-Token": "raw-fixture-secret",
        "X-Binhu-Environment": "shadow", "X-Binhu-Run-Id": "LT-old01",
    })
    assert response.status_code == 503 and calls == []
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    response, calls, _ = await request(make_row())
    assert response.status_code == 503 and calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [
    ({"revision": 9}, "revision_changed"),
    ({"local_revision": 7}, "source_revision_mismatch"),
    ({"local_hash": "f" * 64}, "source_content_mismatch"),
    ({"source_hash": "f" * 64, "local_hash": "f" * 64}, "source_content_mismatch"),
])
async def test_raw_source_fences(setup, change, code):
    response, _, _ = await request(make_row(**change))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code


@pytest.mark.anyio
@pytest.mark.parametrize("row", [
    None, make_row(ref="t_fullchain:28"), make_row(fixture_run="KSHADOW-other"),
    make_row(source_id=10), make_row(parser="出租房屋核查"), make_row(kind="local_dispatch"),
])
async def test_raw_unmarked_or_mismatched_task_is_not_readable(setup, row):
    response, _, _ = await request(row)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_raw_invalid_task_and_environment_are_blocked_before_database(setup):
    response, calls, _ = await request(make_row(), task_id="t_fullchain:9223372036854775808")
    assert response.status_code == 422 and calls == []
    response, calls, _ = await request(make_row(), headers={
        "X-Binhu-Internal-Token": "raw-fixture-secret",
        "X-Binhu-Environment": "shadow", "X-Binhu-Run-Id": "KSHADOW-other",
    })
    assert response.status_code == 403 and calls == []


@pytest.mark.anyio
async def test_raw_selected_nested_body_is_not_serialized(setup):
    response, _, _ = await request(make_row(values={"现住址": {"private": "PRIVATE-NESTED-BODY"}}))
    assert response.status_code == 500 and "PRIVATE-NESTED-BODY" not in response.text


@pytest.mark.anyio
async def test_raw_requested_subset_and_body_like_results(setup):
    response, _, _ = await request(make_row(), fields="task_type")
    assert response.status_code == 200 and response.json()["fields"] == {"task_type": "全链条"}
    response, _, _ = await request(make_row(values={"核查结果": "PRIVATE-FREEFORM-BODY"}))
    assert response.status_code == 422 and "PRIVATE-FREEFORM-BODY" not in response.text
