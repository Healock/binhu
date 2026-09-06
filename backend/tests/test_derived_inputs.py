"""ASGI contract tests for the shadow-only derived-input readback endpoint."""

from __future__ import annotations

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


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, *_args):
        return None


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.execute = AsyncMock()

    def __await__(self):
        async def _return_self():
            return self
        return _return_self().__await__()

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.cursor_instance = _Cursor(row)

    def cursor(self):
        return _CursorContext(self.cursor_instance)


def _app(*, row=None, db_calls=None, cursor_calls=None):
    app = FastAPI()
    app.include_router(derived_inputs.router)

    async def fake_db():
        if db_calls is not None:
            db_calls.append(True)
        connection = _Connection(row)
        if cursor_calls is not None:
            cursor_calls.append(connection.cursor_instance)
        yield connection

    app.dependency_overrides[get_db] = fake_db
    return app


async def _get(app, *, headers=None, params=None, task_id="t_fullchain:27"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            f"/internal/v1/derived-input/tasks/{task_id}",
            headers=headers or {},
            params={"source_id": "9", "revision": "8", "fields": "address,community"} | (params or {}),
        )


@pytest.mark.anyio
async def test_production_is_blocked_before_fastapi_database_dependency(monkeypatch):
    db_calls = []
    app = _app(db_calls=db_calls)
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "production",
        "X-Binhu-Run-Id": "",
    })

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "derived_readback_disabled"
    assert db_calls == []


@pytest.mark.anyio
async def test_shadow_request_requires_exact_environment_and_run_id_before_db(monkeypatch):
    db_calls = []
    app = _app(db_calls=db_calls)
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "production",
        "X-Binhu-Run-Id": "LT-other",
    })

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "shadow_context_mismatch"
    assert db_calls == []


def _row(
    *, source_revision=8, local_revision=8, projection_revision=8,
    values=None, source_kind="local_table", source_ref="t_fullchain:27",
):
    return (
        9,
        "全链条",
        "row-1",
        source_revision,
        "hash-8",
        json.dumps(values or {
            "地址": "虚构路1号",
            "社区": "虚构社区",
            "核查人": "虚构网格员",
            "核查结果": "已核查",
            "身份证号": "FICTIONAL-ID",
        }, ensure_ascii=False),
        json.dumps({"地址": "虚构路1号"}, ensure_ascii=False),
        "虚构社区",
        "虚构小区",
        "虚构网格员",
        "checked",
        projection_revision,
        "虚构路1号",
        datetime(2026, 9, 6, 2, 3, 4),
        local_revision,
        "hash-8",
        source_kind,
        source_ref,
    )


@pytest.mark.anyio
async def test_shadow_readback_query_includes_local_dispatch_sources(monkeypatch):
    cursors = []
    app = _app(row=_row(), cursor_calls=cursors)
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 200
    query, params = cursors[0].execute.await_args.args
    assert "source_kind IN ('local_table','local_dispatch')" in query
    assert "local_source.source_kind IN ('local_table','local_dispatch')" in query
    assert "source.parser_type=%s" in query
    assert params == (9, 27, "全链条")


@pytest.mark.anyio
async def test_shadow_readback_rejects_task_table_that_does_not_match_parser(monkeypatch):
    # The returned row says this is an unrelated parser, while the task ID
    # claims that it belongs to t_fullchain.
    app = _app(row=(_row()[0], "出租房屋核查", *_row()[2:]))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "derived_input_not_found"


@pytest.mark.anyio
async def test_shadow_readback_rejects_non_local_table_source_kind(monkeypatch):
    app = _app(row=_row(
        source_kind="local_dispatch",
        source_ref="police_dispatch_task:11",
    ))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_source_kind"


@pytest.mark.anyio
async def test_shadow_readback_requires_local_table_source_ref_to_match_task_id(monkeypatch):
    app = _app(row=_row(source_ref="t_fullchain:28"))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "derived_input_not_found"


@pytest.mark.anyio
async def test_shadow_readback_rejects_missing_projection_revision(monkeypatch):
    app = _app(row=_row(projection_revision=None))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "projection_revision_mismatch"


@pytest.mark.anyio
async def test_shadow_readback_rejects_task_id_above_int64_before_database(monkeypatch):
    app = _app()
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(
        app,
        task_id="t_fullchain:9223372036854775808",
        headers={
            "X-Binhu-Internal-Token": "secret",
            "X-Binhu-Environment": "shadow",
            "X-Binhu-Run-Id": "LT-fixture-01",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_local_task_id"


@pytest.mark.anyio
async def test_shadow_readback_maps_real_chinese_columns_and_only_returns_requested_fields(monkeypatch):
    app = _app(row=_row())
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t_fullchain:27"
    assert body["fields"] == {
        "address": "虚构路1号",
        "community": "虚构社区",
    }
    assert "inspector" not in body["fields"]
    assert "身份证号" not in response.text
    assert body["updated_at"].endswith("Z")
    assert body["environment"] == "shadow"
    assert body["run_id"] == "LT-fixture-01"


@pytest.mark.anyio
async def test_shadow_readback_rejects_projection_revision_fence(monkeypatch):
    app = _app(row=_row(projection_revision=7))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "projection_revision_mismatch"


@pytest.mark.anyio
async def test_shadow_readback_rejects_nested_selected_values(monkeypatch):
    app = _app(row=_row(values={"地址": {"unsafe": "value"}, "社区": "虚构社区"}))
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(settings, "LOAD_TEST_RUN_ID", "LT-fixture-01")
    monkeypatch.setattr(settings, "DERIVED_READBACK_TOKEN", "secret")

    response = await _get(app, headers={
        "X-Binhu-Internal-Token": "secret",
        "X-Binhu-Environment": "shadow",
        "X-Binhu-Run-Id": "LT-fixture-01",
    })

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "derived_input_invalid_value"
