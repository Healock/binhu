"""Contract tests for metadata-only events and the derived-input readback API.

These tests use an in-memory cursor double.  They deliberately do not require
MySQL, Docker, Kafka, Redis, or any real business data.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# Keep this contract suite independent of a developer's local .env file.
os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.derived_inputs import read_derived_input
from services.domain_events import decode_event_row, enqueue_event


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, *_args):
        return None


class _Cursor:
    def __init__(self, row=None):
        self.execute = AsyncMock()
        self.fetchone = AsyncMock(return_value=row)


class _Connection:
    def __init__(self, row=None):
        self.cursor_instance = _Cursor(row)

    def cursor(self):
        return _CursorContext(self.cursor_instance)


def _old_event_row():
    """The column order emitted before metadata columns were introduced."""
    return (
        "event-old", 1, "online", "online.task.changed", "online_task",
        "fullchain:old", 4, '["authenticated"]', "pending", 0, None, None,
        None, "", "", datetime(2026, 9, 6, 1, 2, 3), None,
    )


def _new_event_row(changed_fields='["address","task_state"]'):
    return _old_event_row() + (
        "task-1", 27, "operation-1", changed_fields,
    )


def test_decode_event_row_accepts_old_rows_without_new_metadata_columns():
    event = decode_event_row(_old_event_row())

    assert event["event_id"] == "event-old"
    assert event["schema_version"] == 1
    assert event["task_id"] is None
    assert event["source_id"] is None
    assert event["operation_id"] is None
    assert event["changed_fields"] == []


def test_decode_event_row_decodes_new_metadata_and_keeps_event_body_free_of_values():
    event = decode_event_row(_new_event_row())

    assert event["schema_version"] == 1
    assert event["task_id"] == "task-1"
    assert event["source_id"] == 27
    assert event["operation_id"] == "operation-1"
    assert event["changed_fields"] == ["address", "task_state"]
    assert "values_json" not in event
    assert "身份证号" not in json.dumps(event, ensure_ascii=False)


def test_decode_event_row_rejects_malformed_or_unsafe_changed_fields():
    for value in (
        "{\"address\": true}",
        "not-json",
        '["address\\nleak"]',
        '["身份证号=123456789012345678"]',
        json.dumps(["field"] * 65),
    ):
        with pytest.raises(ValueError):
            decode_event_row(_new_event_row(value))


def test_enqueue_event_normalises_changed_fields_and_never_accepts_a_string():
    cursor = _Cursor()

    asyncio.run(enqueue_event(
        cursor,
        domain="online",
        event_type="online.task.changed",
        aggregate_type="online_task",
        aggregate_id="task-1",
        aggregate_revision=3,
        audiences=["authenticated"],
        task_id="task-1",
        source_id=27,
        operation_id=" operation-1 ",
        changed_fields=["task_state", "address", "address"],
    ))

    params = cursor.execute.await_args.args[1]
    assert params[8:12] == (
        "task-1", 27, "operation-1", '["address","task_state"]',
    )
    assert "values_json" not in cursor.execute.await_args.args[0]

    with pytest.raises(ValueError):
        asyncio.run(enqueue_event(
            cursor,
            domain="online",
            event_type="online.task.changed",
            aggregate_type="online_task",
            aggregate_id="task-1",
            aggregate_revision=3,
            audiences=["authenticated"],
            changed_fields="address",
        ))


def test_derived_readback_token_is_fail_closed_before_database_access():
    conn = _Connection()

    with patch("routers.derived_inputs.settings.DERIVED_READBACK_TOKEN", ""):
        with pytest.raises(HTTPException) as error:
            asyncio.run(read_derived_input("task-1", None, None, None, "", conn))
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "derived_readback_disabled"
    conn.cursor_instance.execute.assert_not_awaited()

    with patch("routers.derived_inputs.settings.DERIVED_READBACK_TOKEN", "secret"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(read_derived_input("task-1", "wrong", None, None, "", conn))
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "invalid_internal_credential"
    conn.cursor_instance.execute.assert_not_awaited()


def test_derived_readback_rejects_unknown_fields_before_query():
    conn = _Connection()
    with patch("routers.derived_inputs.settings.DERIVED_READBACK_TOKEN", "secret"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(read_derived_input(
                "task-1", "secret", None, None, "address,身份证号", conn,
            ))
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_readback_field"
    assert error.value.detail["fields"] == ["身份证号"]
    conn.cursor_instance.execute.assert_not_awaited()


def test_derived_readback_returns_allowlisted_fields_and_checks_revision():
    row = (
        27, "全链条", "row-1", 8, "hash-8",
        json.dumps({
            "address": "虚构路1号",
            "身份证号": "FICTIONAL-SENSITIVE-VALUE",
            "手机号": "FICTIONAL-PHONE",
        }, ensure_ascii=False),
        json.dumps({"small_community": "虚构小区", "task_type": "核查"}, ensure_ascii=False),
        "虚构社区", "pending", datetime(2026, 9, 6, 2, 3, 4),
    )
    conn = _Connection(row)

    with patch("routers.derived_inputs.settings.DERIVED_READBACK_TOKEN", "secret"):
        result = asyncio.run(read_derived_input(
            "task-1", "secret", 27, 8, "address,small_community", conn,
        ))
    assert result["task_id"] == "task-1"
    assert result["source_id"] == 27
    assert result["revision"] == 8
    assert result["fields"] == {
        "address": "虚构路1号",
        "small_community": "虚构小区",
        "community": "虚构社区",
        "task_state": "pending",
    }
    encoded = json.dumps(result, ensure_ascii=False)
    assert "身份证号" not in encoded
    assert "手机号" not in encoded
    assert result["readback_hash"]

    conn = _Connection(row)
    with patch("routers.derived_inputs.settings.DERIVED_READBACK_TOKEN", "secret"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(read_derived_input(
                "task-1", "secret", 27, 7, "address", conn,
            ))
    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "revision_changed",
        "task_id": "task-1",
        "current_revision": 8,
    }
