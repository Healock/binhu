import os

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
