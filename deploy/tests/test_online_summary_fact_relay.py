import json

import pytest

from deploy.environments.event_pipeline.services.online_summary_fact_relay import OnlineSummaryFactRelay


FACT = {
    "contract_version": 1,
    "event_type": "summary.task.changed",
    "event_id": "11111111-1111-1111-1111-111111111111",
    "command_id": "22222222-2222-2222-2222-222222222222",
    "operation_id": "33333333-3333-3333-3333-333333333333",
    "environment": "development",
    "run_id": "dev-summary-1",
    "task_id": "t_fullchain:1",
    "source_id": 1,
    "revision": 2,
    "task_revision": 2,
    "business_date": "2026-11-05",
    "parser_type": "全链条",
    "row_key_hmac": "c" * 64,
    "community_token": "a" * 64,
    "inspector_token": "b" * 64,
    "task_state": "completed",
    "included": 1,
    "online_present": 1,
    "unable_to_verify": 0,
    "reached_bottom": 0,
    "effective_workload": 1,
    "activity_kind": "state_transition",
    "correction": False,
    "deleted": False,
    "occurred_at": "2026-11-05T00:00:00Z",
}


class Cursor:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, params=None):
        self.pool.calls.append((query, params))

    async def fetchone(self):
        return (FACT["event_id"], json.dumps(FACT), "pending", 0)


class Connection:
    def __init__(self, pool):
        self.pool = pool

    def cursor(self):
        return Cursor(self.pool)

    async def begin(self):
        self.pool.transactions.append("begin")

    async def commit(self):
        self.pool.transactions.append("commit")

    async def rollback(self):
        self.pool.transactions.append("rollback")


class Pool:
    def __init__(self):
        self.calls = []
        self.transactions = []

    async def __aenter__(self):
        return Connection(self)

    async def __aexit__(self, *_args):
        return False

    def acquire(self):
        return self


class Redis:
    def __init__(self, fail=False):
        self.fail = fail
        self.entries = []

    async def xadd(self, stream, values, **kwargs):
        if self.fail:
            raise ConnectionError("not exposed")
        self.entries.append((stream, values, kwargs))


@pytest.mark.asyncio
async def test_fact_outbox_is_leased_published_then_acknowledged():
    pool, redis = Pool(), Redis()
    relay = OnlineSummaryFactRelay(
        pool, redis, stream="binhu:events", environment="development",
        run_id="dev-summary-1", worker_id="test-relay",
    )

    assert await relay.run_once() == "published"
    assert pool.transactions == ["begin", "commit", "commit"]
    assert len(redis.entries) == 1
    stream, values, _options = redis.entries[0]
    assert stream == "binhu:events"
    envelope = json.loads(values["event"])
    assert envelope["event_type"] == "online.summary.fact"
    assert envelope["summary_fact"]["event_id"] == FACT["event_id"]
    assert any(params and params[0] == "published" for query, params in pool.calls)


@pytest.mark.asyncio
async def test_fact_outbox_retries_redis_failure_without_logging_payload():
    pool, redis = Pool(), Redis(fail=True)
    relay = OnlineSummaryFactRelay(
        pool, redis, stream="binhu:events", environment="development",
        run_id="dev-summary-1", worker_id="test-relay",
    )

    assert await relay.run_once() == "retry"
    finish = [params for query, params in pool.calls if "SET status=%s" in query]
    assert finish[-1][0] == "retry"
    assert finish[-1][1] == "redis_publish_failed"


def test_fact_outbox_rejects_production_identity():
    with pytest.raises(ValueError):
        OnlineSummaryFactRelay(
            Pool(), Redis(), stream="binhu:events", environment="production",
            run_id="prod-run",
        )
