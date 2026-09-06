"""Real Redis fencing/expiry component check; synthetic fixtures, exact scoped cleanup."""
import asyncio
import json
import os
import secrets
from urllib.parse import urlparse
import redis.asyncio as redis
from relay_runtime import configuration, connect_shadow
from services.derived_revision_cache import RevisionCache


async def main():
    config = configuration()
    target = urlparse(os.environ["REDIS_URL"])
    if (target.scheme != "redis" or target.hostname != "derived-redis"
            or target.port != 6379 or target.path != "/0" or not target.password):
        raise ValueError("isolated Redis target required")
    pool = await connect_shadow(config)
    pool.close()
    await pool.wait_closed()
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                 socket_timeout=5, socket_connect_timeout=5)
    cache = RevisionCache(client, config["run_id"], ttl_seconds=60)
    task = 920000000 + secrets.randbelow(100000000)
    base = dict(task_id=f"t_fullchain:{task}", source_id=task, revision=2**63-2,
                source_revision=2**63-2, generated_at="2026-09-07T00:00:00Z",
                source="flink-shadow", content_hash="a"*64, fields={"task_state":"synthetic"})
    keys = set()
    for source in ("flink-shadow", "python-shadow"):
        for revision in (5,2**63-2,2**63-1):
            keys.update(cache.keys(base["task_id"], task, source, revision))
    try:
        assert not any([await client.exists(k) for k in keys]), "fixture collision"
        outcomes = {"high":await cache.put(base),
                    "old":await cache.put(dict(base, revision=5, source_revision=5)),
                    "duplicate":await cache.put(base),
                    "conflict":await cache.put(dict(base, fields={"task_state":"changed"}))}
        assert outcomes == dict(high="updated", old="stale", duplicate="duplicate", conflict="conflict")
        latest, snapshot = cache.keys(base["task_id"],task,base["source"],base["revision"])
        assert await client.ttl(latest) == -1
        assert 0 < await client.ttl(snapshot) <= 60
        value = await cache.get(base["task_id"],task,base["source"],base["revision"])
        assert value["revision"] == str(2**63-2)
        assert await cache.get(base["task_id"],task,base["source"],5) is None
        print(json.dumps({"phase":"expiry_wait", "seconds":61}), flush=True)
        await asyncio.sleep(61)
        assert await client.exists(snapshot) == 0
        assert await client.ttl(latest) == -1
        assert await cache.get(base["task_id"],task,base["source"],base["revision"]) is None
        assert await cache.put(dict(base,revision=5,source_revision=5)) == "stale"
        assert await cache.put(base) == "duplicate"
        assert await cache.get(base["task_id"],task,base["source"],base["revision"]) == value
        other = dict(base, source="python-shadow")
        assert await cache.put(other) == "updated"
        other_value = await cache.get(base["task_id"],task,"python-shadow",base["revision"])
        assert other_value["result_hash"] == value["result_hash"]
        maximum = dict(base,revision=2**63-1,source_revision=2**63-1)
        assert await cache.put(maximum) == "updated"
        assert await cache.put(base) == "stale"
        assert await cache.get(base["task_id"],task,base["source"],base["revision"]) is None
        print(json.dumps({"status":"passed","scope":"real_redis_component","run_id":config["run_id"],
                          "results":outcomes,"max_revision":str(2**63-1),"actual_expiry_seconds":61,
                          "persistent_fence":True,"rebuild":True,"independent_tracks":True}),flush=True)
    finally:
        # Only the exact randomly generated keys from this isolated run.
        await client.unlink(*sorted(keys))
        assert not any([await client.exists(k) for k in keys])
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
