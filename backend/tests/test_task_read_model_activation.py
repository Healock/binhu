import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

import pytest

from migrations import task_read_model_activation as activation


class _Redis:
    def __init__(self):
        self.values = {}
        self.deleted = []
        self.closed = False

    async def set(self, key, value):
        self.values[key] = value

    async def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)

    async def aclose(self):
        self.closed = True


def test_activation_detects_extra_task_index_memberships():
    expected = {"state-index-a": {"t_fullchain:1"}}
    actual = {
        "state-index-a": {"t_fullchain:1", "t_fullchain:2"},
        "orphan-state-index": {"t_fullchain:3"},
    }

    assert activation._index_membership_mismatch_count(expected, actual) == 2


@pytest.mark.asyncio
async def test_measure_does_not_change_readiness_markers(monkeypatch):
    client = _Redis()
    client.values.update({
        "test-prefix:state": "ready:dev-run",
        "test-prefix:state-index-version": "v1",
    })

    async def check_generation():
        return ({"run_id": "dev-run", "ready": True}, client, "test-prefix")

    monkeypatch.setattr(activation, "_check_generation", check_generation)

    result = await activation.run("measure")

    assert result["readiness_marked"] is False
    assert client.values == {
        "test-prefix:state": "ready:dev-run",
        "test-prefix:state-index-version": "v1",
    }
    assert client.deleted == []
    assert client.closed is True


@pytest.mark.asyncio
async def test_verify_marks_task_and_state_indexes_ready_only_after_clean_measure(monkeypatch):
    client = _Redis()

    async def check_generation():
        return ({"run_id": "dev-run", "ready": True}, client, "test-prefix")

    monkeypatch.setattr(activation, "_check_generation", check_generation)

    result = await activation.run("verify")

    assert result["readiness_marked"] is True
    assert client.values == {
        "test-prefix:state-index-version": "v1",
        "test-prefix:state": "ready:dev-run",
    }
    assert client.closed is True


@pytest.mark.asyncio
async def test_failed_verify_clears_both_readiness_markers(monkeypatch):
    client = _Redis()
    client.values.update({
        "test-prefix:state": "ready:dev-run",
        "test-prefix:state-index-version": "v1",
    })

    async def check_generation():
        return ({"run_id": "dev-run", "ready": False}, client, "test-prefix")

    monkeypatch.setattr(activation, "_check_generation", check_generation)

    result = await activation.run("verify")

    assert result["readiness_marked"] is False
    assert client.values == {}
    assert set(client.deleted) == {
        "test-prefix:state-index-version",
        "test-prefix:state",
    }
    assert client.closed is True


@pytest.mark.asyncio
async def test_measure_compares_mysql_sources_with_redis_task_index(monkeypatch):
    environment, run_id = "development", "dev-read-model-measure"
    task_id = "t_fullchain:1"
    _, _, prefix = activation.task_read_model_keys(environment, run_id, task_id)
    cursor = AsyncMock()
    cursor.fetchall.return_value = [(1, 1, 2, "synthetic-row")]
    mysql = MagicMock()
    mysql.cursor.return_value.__aenter__.return_value = cursor
    store = MagicMock()
    store.ping = AsyncMock()
    store.zrange = AsyncMock(return_value=[])
    store.aclose = AsyncMock()
    store.pipeline.return_value.execute = AsyncMock(return_value=[{}])

    async def scan_iter(**_):
        for key in ():
            yield key

    store.scan_iter.side_effect = scan_iter
    monkeypatch.setattr(activation, "_identity", lambda: (environment, run_id))
    monkeypatch.setattr(activation, "PARSER_REGISTRY", {
        "synthetic": SimpleNamespace(table_name="t_fullchain"),
    })
    monkeypatch.setattr(activation.aiomysql, "connect", AsyncMock(return_value=mysql))
    monkeypatch.setattr(activation.redis, "from_url", lambda *args, **kwargs: store)

    report = await activation.run("measure")

    assert report["ready"] is False
    assert report["parsers"] == [{
        "parser_type": "synthetic",
        "mysql_active_sources": 1,
        "redis_indexed_tasks": 0,
        "revision_or_identity_mismatches": 1,
        "state_index_mismatches": 0,
        "membership_matches": False,
    }]
    index = activation.index_key(environment, run_id, "task_type", "t_fullchain",
                                 secret=activation.settings.TASK_EVENT_ENCRYPTION_KEY)
    assert index.startswith(prefix)
    store.zrange.assert_awaited_once_with(index, 0, -1)
    store.set.assert_not_called()
    store.delete.assert_not_called()
    mysql.close.assert_called_once()
    store.aclose.assert_awaited_once()
