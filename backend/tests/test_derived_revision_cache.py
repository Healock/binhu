import asyncio
import json
import pytest
from services.derived_revision_cache import RevisionCache, CacheContractError


class RedisDouble:
    def __init__(self, outcome=1):
        self.calls = []
        self.outcome = outcome

    async def eval(self, script, count, *args):
        self.calls.append((script, count, args))
        return self.outcome


def result(revision=5):
    return dict(task_id="t_fullchain:9", source_id=9, revision=revision,
                source_revision=revision, generated_at="2026-09-06T10:00:00Z",
                source="flink-shadow", content_hash="a" * 64,
                fields={"task_state": "synthetic"})


def test_scoped_snapshot_and_pointer_share_hash_slot():
    redis = RedisDouble()
    assert asyncio.run(RevisionCache(redis, "KSHADOW-test").put(result())) == "updated"
    _, count, args = redis.calls[0]
    assert count == 2
    assert args[0].endswith(":latest") and args[1].endswith(":revision:5")
    assert args[0].split("}")[0] == args[1].split("}")[0]
    payload = json.loads(args[5])
    assert payload["environment"] == "shadow" and payload["run_id"] == "KSHADOW-test"
    assert payload["revision"] == "5"


@pytest.mark.parametrize("code,status", [(0,"stale"), (-1,"conflict"), (2,"duplicate")])
def test_returns_real_redis_outcome_without_simulating_lua(code, status):
    assert asyncio.run(RevisionCache(RedisDouble(code), "KSHADOW-test").put(result())) == status


@pytest.mark.parametrize("change", [
    {"source_revision": 4}, {"source_revision": True}, {"revision": True},
    {"revision": 2**63, "source_revision": 2**63}, {"source_id": True},
    {"task_id": "t_fullchain:9}:escape"}, {"generated_at": "2026-09-06"},
    {"source": "arbitrary"}, {"content_hash": "unbounded"},
    {"fields": {"task_state": {}}}, {"fields": {"phone": "synthetic"}},
    {"fields": {"task_state": float("nan")}}, {"extra": "body"},
])
def test_invalid_contract_never_calls_redis(change):
    redis = RedisDouble()
    with pytest.raises(CacheContractError):
        asyncio.run(RevisionCache(redis, "KSHADOW-test").put(dict(result(), **change)))
    assert not redis.calls


def test_hash_covers_result_and_ignores_generation_time():
    redis = RedisDouble(); cache = RevisionCache(redis, "KSHADOW-test")
    asyncio.run(cache.put(result()))
    asyncio.run(cache.put(dict(result(), generated_at="2026-09-06T11:00:00Z")))
    assert redis.calls[0][2][4] == redis.calls[1][2][4]
    asyncio.run(cache.put(dict(result(), fields={"task_state":"different"})))
    assert redis.calls[0][2][4] != redis.calls[2][2][4]
