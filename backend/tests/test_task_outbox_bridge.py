from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from config import Settings
from services import task_outbox_bridge


def _event_settings(**changes):
    values = {
        "MYSQL_PASSWORD": "test-password",
        "ENCRYPTION_KEY": "test-encryption-key",
        "APP_ENVIRONMENT": "shadow",
        "SESSION_COOKIE_NAME": "binhu_shadow_session",
        "LOAD_TEST_RUN_ID": "KSHADOW-fixture-01",
        "KAFKA_TASK_EVENTS_ENABLED": True,
    }
    values.update(changes)
    return Settings(**values)


def test_task_event_bridge_is_strictly_disabled_by_default():
    settings = _event_settings(KAFKA_TASK_EVENTS_ENABLED=False)
    assert task_outbox_bridge.bridge_config(settings) is None


def test_task_event_bridge_requires_shadow_and_kshadow_run_id():
    with pytest.raises(ValueError):
        _event_settings(
            APP_ENVIRONMENT="production",
            SESSION_COOKIE_NAME="binhu_session",
            KAFKA_TASK_EVENTS_ENABLED=True,
        )
    with pytest.raises(ValueError):
        _event_settings(LOAD_TEST_RUN_ID="LT-fixture-01")
    with pytest.raises(ValueError):
        task_outbox_bridge.bridge_config(_event_settings(LOAD_TEST_RUN_ID="KSHADOW-"))


def test_build_task_event_uses_stable_local_task_source_and_revision_metadata():
    settings = _event_settings()
    event = task_outbox_bridge.build_task_event(
        settings,
        event_id="00000000-0000-0000-0000-000000000001",
        event_type="online.task.changed",
        task_id="t_fullchain:27",
        source_id=44,
        revision=9,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["核查人", "地址", "地址"],
        occurred_at=datetime(2026, 9, 7, 1, 2, 3, tzinfo=timezone.utc),
    )
    assert event == {
        "schema_version": 1,
        "event_id": "00000000-0000-0000-0000-000000000001",
        "event_type": "task.saved",
        "task_id": "t_fullchain:27",
        "source_id": 44,
        "revision": 9,
        "operation_id": "00000000-0000-0000-0000-000000000002",
        "changed_fields": ["address", "inspector"],
        "timestamp": "2026-09-07T01:02:03.000000Z",
        "environment": "shadow",
        "run_id": "KSHADOW-fixture-01",
    }


def test_build_task_event_preserves_strict_integer_contract_and_delete_semantics():
    settings = _event_settings()
    with pytest.raises(ValueError):
        task_outbox_bridge.build_task_event(
            settings,
            event_id="00000000-0000-0000-0000-000000000001",
            event_type="online.task.changed",
            task_id="t_fullchain:27",
            source_id=True,
            revision=9,
            operation_id="00000000-0000-0000-0000-000000000002",
            changed_fields=["地址"],
        )
    event = task_outbox_bridge.build_task_event(
        settings,
        event_id="00000000-0000-0000-0000-000000000001",
        event_type="online.task.deleted",
        task_id="t_fullchain:27",
        source_id=44,
        revision=9,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["任务状态"],
    )
    assert event["event_type"] == "task.deleted"


def test_archive_override_requires_explicit_delete_context():
    kwargs = dict(
        event_id="00000000-0000-0000-0000-000000000001",
        event_type="online.task.deleted", task_id="t_fullchain:27",
        source_id=44, revision=9,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["task_state"], kafka_event_type="task.archived",
    )
    assert task_outbox_bridge.build_task_event(_event_settings(), **kwargs)["event_type"] == "task.archived"
    kwargs["event_type"] = "online.task.changed"
    with pytest.raises(ValueError):
        task_outbox_bridge.build_task_event(_event_settings(), **kwargs)


def test_enqueue_task_event_binds_delivery_to_the_callers_cursor(monkeypatch):
    cursor = object()
    enqueue = AsyncMock(return_value="event-id")
    monkeypatch.setattr(task_outbox_bridge, "enqueue_event", enqueue)
    settings = _event_settings()

    result = __import__("asyncio").run(task_outbox_bridge.enqueue_task_event(
        cursor,
        settings=settings,
        domain="online",
        event_type="online.task.changed",
        aggregate_type="online_task",
        aggregate_id="全链条:row-key",
        aggregate_revision=9,
        audiences=["authenticated"],
        task_id="t_fullchain:27",
        source_id=44,
        revision=9,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["address"],
    ))

    assert result == "event-id"
    kwargs = enqueue.await_args.kwargs
    assert kwargs["kafka_run_id"] == "KSHADOW-fixture-01"
    assert kwargs["kafka_event"]["event_id"] == kwargs["event_id"]
    assert enqueue.await_args.args[0] is cursor


def test_enqueue_task_event_accepts_explicit_event_id(monkeypatch):
    cursor = object()
    enqueue = AsyncMock(return_value="event-id")
    monkeypatch.setattr(task_outbox_bridge, "enqueue_event", enqueue)
    settings = _event_settings()

    __import__("asyncio").run(task_outbox_bridge.enqueue_task_event(
        cursor,
        settings=settings,
        domain="online",
        event_type="online.task.changed",
        aggregate_type="online_task",
        aggregate_id="fullchain:row-key",
        aggregate_revision=9,
        audiences=["authenticated"],
        task_id="t_fullchain:27",
        source_id=44,
        revision=9,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["address"],
        event_id="00000000-0000-0000-0000-000000000001",
    ))

    assert enqueue.await_args.kwargs["event_id"] == (
        "00000000-0000-0000-0000-000000000001"
    )


def test_enqueue_task_event_does_not_create_kafka_delivery_when_disabled(monkeypatch):
    cursor = object()
    enqueue = AsyncMock(return_value="event-id")
    monkeypatch.setattr(task_outbox_bridge, "enqueue_event", enqueue)
    settings = _event_settings(KAFKA_TASK_EVENTS_ENABLED=False)

    __import__("asyncio").run(task_outbox_bridge.enqueue_task_event(
        cursor,
        settings=settings,
        domain="online",
        event_type="online.task.created",
        aggregate_type="online_task",
        aggregate_id="全链条:row-key",
        aggregate_revision=1,
        audiences=["authenticated"],
        task_id="t_fullchain:27",
        source_id=44,
        revision=1,
        operation_id="00000000-0000-0000-0000-000000000002",
        changed_fields=["task_state"],
    ))

    kwargs = enqueue.await_args.kwargs
    assert kwargs["kafka_event"] is None
    assert kwargs["kafka_run_id"] is None
