import asyncio,json,os
import redis.asyncio as redis
from services.derived_revision_cache import RevisionCache
async def main():
 c=redis.Redis.from_url(os.environ["REDIS_URL"],decode_responses=True)
 cache=RevisionCache(c,os.environ["LOAD_TEST_RUN_ID"],ttl_seconds=60)
 h=lambda n: n*64
 b={"task_id":"t_fullchain:920001","source_id":920001,"source_revision":2**63-2,"revision":2**63-2,"generated_at":"2026-09-06T10:00:00Z","source":"flink-shadow","content_hash":h("a"),"fields":{"task_state":"synthetic"}}
 out={"high":await cache.put(b),"old":await cache.put(dict(b,revision=5,source_revision=5,content_hash=h("b"))),"dup":await cache.put(b),"conflict":await cache.put(dict(b,fields={"task_state":"other"},content_hash=h("c")))}
 key,_=cache.keys(b["task_id"],b["source_id"],b["source"],b["revision"])
 stored=await c.hgetall(key); assert out=={"high":"updated","old":"stale","dup":"duplicate","conflict":"conflict"}; assert stored["revision"]==str(2**63-2)
 print(json.dumps({"status":"passed","results":out,"high_watermark":stored["revision"],"snapshot_ttl":await c.ttl(cache.keys(b["task_id"],b["source_id"],b["source"],b["revision"])[1])}))
 await c.aclose()
asyncio.run(main())
