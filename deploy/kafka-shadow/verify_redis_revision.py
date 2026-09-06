import asyncio,json,os
import redis.asyncio as redis
from services.derived_revision_cache import RevisionCache

async def main():
    client=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
    cache=RevisionCache(client,os.environ['LOAD_TEST_RUN_ID'],ttl_seconds=3600)
    base={'task_id':'t_fullchain:920001','source_id':920001,'source_revision':5,'revision':5,
          'generated_at':'2026-09-06T10:00:00Z','source':'flink-shadow','content_hash':'r5',
          'fields':{'task_state':'synthetic'}}
    out={'r5':await cache.put(base),'old':await cache.put(dict(base,revision=4,source_revision=4,content_hash='r4')),
         'conflict':await cache.put(dict(base,content_hash='other')),
         'r6':await cache.put(dict(base,revision=6,source_revision=6,content_hash='r6'))}
    key=f"binhu:shadow:{os.environ['LOAD_TEST_RUN_ID']}:derived:{base['task_id']}:{base['source_id']}"
    stored=await client.hgetall(key); latest=await client.get(key+':latest')
    assert out=={'r5':'updated','old':'stale','conflict':'conflict','r6':'updated'}
    assert stored['revision']=='6' and stored['content_hash']=='r6' and latest=='6|r6'
    print(json.dumps({'status':'passed','results':out,'stored_revision':stored['revision'],'latest':latest}))
    await client.aclose()
if __name__=='__main__': asyncio.run(main())
