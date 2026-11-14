from unittest.mock import MagicMock, patch

import pytest

from services import ops_overview


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query):
        self.query = query

    async def fetchall(self):
        assert "payload" not in self.query.lower()
        assert "aggregate_id" not in self.query.lower()
        assert "interval 1 day" in self.query.lower()
        return [
            ("online", "pending", 4, 120),
            ("online", "published", 23, 0),
            ("online", "dead_letter", 1, 0),
        ]


class _Pool:
    def __init__(self):
        self.cursor = _Cursor()
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor

    async def acquire(self):
        return self.conn

    def release(self, _conn):
        return None


class _Redis:
    def __init__(self, healthy=True):
        self.healthy = healthy

    async def ping(self):
        if not self.healthy:
            raise ConnectionError("unavailable")

    async def aclose(self):
        return None


def _component(snapshot, component_id):
    return next(item for item in snapshot["components"] if item["id"] == component_id)


@pytest.mark.asyncio
async def test_task_pipeline_snapshot_reports_real_components_and_safe_outbox_aggregates():
    pool = _Pool()
    redis_client = _Redis()
    containers = [
        {"source": "redis", "status": "running", "restart_count": 0},
        {"source": "event-relay", "status": "running", "restart_count": 1},
    ]
    with patch.object(ops_overview.db_manager, "get_pool", return_value=pool), \
            patch("redis.asyncio.from_url", return_value=redis_client):
        snapshot = await ops_overview.build_task_pipeline_snapshot(
            containers, {"connected": True}
        )

    assert snapshot["environment"] == ops_overview.settings.APP_ENVIRONMENT
    assert _component(snapshot, "redis")["status"] == "healthy"
    assert _component(snapshot, "event-relay")["status"] == "healthy"
    assert _component(snapshot, "kafka")["status"] == "not_deployed"
    assert _component(snapshot, "flink")["status"] == "not_deployed"
    assert _component(snapshot, "task-read-model")["status"] == "disabled"
    assert _component(snapshot, "outbox")["details"] == {
        "pending": 8,
        "blocked_or_dead_letter": 2,
        "oldest_pending_seconds": 120,
    }
    assert snapshot["outbox"]["domains"] == [
        {
            "domain": "online_data:online",
            "statuses": {"pending": 4, "published": 23, "dead_letter": 1},
            "pending": 4,
            "oldest_pending_seconds": 120,
        },
        {
            "domain": "platform:online",
            "statuses": {"pending": 4, "published": 23, "dead_letter": 1},
            "pending": 4,
            "oldest_pending_seconds": 120,
        },
    ]


@pytest.mark.asyncio
async def test_pipeline_snapshot_fails_closed_when_redis_or_outbox_cannot_be_read():
    redis_client = _Redis(healthy=False)
    with patch.object(ops_overview.db_manager, "get_pool", side_effect=ValueError("missing")), \
            patch("redis.asyncio.from_url", return_value=redis_client):
        snapshot = await ops_overview.build_task_pipeline_snapshot(
            [{"source": "redis", "status": "running"}], {"connected": True}
        )

    assert snapshot["outbox"]["available"] is False
    assert _component(snapshot, "outbox")["status"] == "unavailable"
    assert _component(snapshot, "redis")["status"] == "unavailable"
    assert _component(snapshot, "task-read-model")["status"] == "disabled"
