"""Revision fenced Redis result cache for isolated shadow derived consumers."""
from __future__ import annotations
import hashlib, json, math, re
from datetime import datetime
from services.kafka_event_contract import _validate_task_id, EventContractError

class CacheContractError(ValueError): pass
_RUN=re.compile(r"^KSHADOW-[A-Za-z0-9_-]{1,64}$")
_HASH=re.compile(r"^[0-9a-fA-F]{64}$")
_FIELDS={"task_state","address_match","person_tags","task_graph","daily_count"}
_SOURCES={"python-shadow","flink-shadow"}
_LUA="""local old=redis.call('HGET',KEYS[1],'revision')
local incoming=ARGV[1]
if old and (#incoming < #old or (#incoming == #old and incoming < old)) then return 0 end
if old and incoming == old then
  local h=redis.call('HGET',KEYS[1],'result_hash')
  if h ~= ARGV[3] then return -1 end
  if redis.call('EXISTS',KEYS[2]) == 0 then
    redis.call('SET',KEYS[2],ARGV[4],'EX',ARGV[2])
  end
  return 2
end
redis.call('HSET',KEYS[1],'revision',incoming,'result_hash',ARGV[3],
  'generated_at',ARGV[5],'source_id',ARGV[6],'source',ARGV[7])
redis.call('SET',KEYS[2],ARGV[4],'EX',ARGV[2])
return 1"""
_READ_LUA="""if redis.call('HGET',KEYS[1],'revision') ~= ARGV[1] then return nil end
return redis.call('GET',KEYS[2])"""

def _validate_timestamp(v):
    if not isinstance(v,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",v): raise CacheContractError("generated_at")
    try: datetime.fromisoformat(v.replace("Z","+00:00"))
    except ValueError: raise CacheContractError("generated_at")

def _scalar(v,k):
    if isinstance(v,bool) or not isinstance(v,(str,int,float)) or (isinstance(v,float) and not math.isfinite(v)): raise CacheContractError(k)

def _hash_result(r):
    body={"task_id":r["task_id"],"source_id":r["source_id"],"revision":str(r["revision"]),"source_revision":str(r["source_revision"]),"content_hash":r["content_hash"].lower(),"fields":r["fields"]}
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

class RevisionCache:
    def __init__(self,redis,run_id,ttl_seconds=86400):
        if not isinstance(run_id,str) or not _RUN.fullmatch(run_id) or type(ttl_seconds) is not int or not 60<=ttl_seconds<=604800: raise CacheContractError("scope")
        self.redis,self.run_id,self.ttl=redis,run_id,ttl_seconds
    def keys(self, task_id, source_id, source, revision):
        try: _validate_task_id(task_id)
        except EventContractError: raise CacheContractError("task_id") from None
        if type(source_id) is not int or not 0 < source_id < 2**63: raise CacheContractError("source_id")
        if not isinstance(source,str) or source not in _SOURCES: raise CacheContractError("source")
        if type(revision) is not int or not 0 <= revision < 2**63: raise CacheContractError("revision")
        tag=f"{{binhu:shadow:{self.run_id}:derived:{source}:{task_id}:{source_id}}}"
        return tag+":latest", tag+":revision:"+str(revision)
    async def put(self,result):
        if not isinstance(result,dict) or set(result)-{"task_id","source_id","revision","source_revision","generated_at","source","content_hash","fields"}: raise CacheContractError("envelope")
        if type(result.get("revision")) is not int or not 0<=result["revision"]<2**63 or type(result.get("source_revision")) is not int or result["source_revision"]!=result["revision"]: raise CacheContractError("revision")
        latest,snap=self.keys(result.get("task_id"),result.get("source_id"),result.get("source"),result["revision"])
        _validate_timestamp(result.get("generated_at"))
        if not isinstance(result.get("content_hash"),str) or not _HASH.fullmatch(result["content_hash"]): raise CacheContractError("content_hash")
        fields=result.get("fields")
        if not isinstance(fields,dict) or set(fields)-_FIELDS: raise CacheContractError("fields")
        for k,v in fields.items(): _scalar(v,k)
        result_hash=_hash_result(result)
        payload={"environment":"shadow","run_id":self.run_id,"task_id":result["task_id"],"source_id":result["source_id"],"revision":str(result["revision"]),"source_revision":str(result["source_revision"]),"generated_at":result["generated_at"],"source":result["source"],"content_hash":result["content_hash"],"fields":fields,"result_hash":result_hash}
        raw=json.dumps(payload,ensure_ascii=False,separators=(",",":"),allow_nan=False)
        if len(raw.encode())>65536: raise CacheContractError("size")
        out=await self.redis.eval(_LUA,2,latest,snap,str(result["revision"]),str(self.ttl),result_hash,raw,result["generated_at"],str(result["source_id"]),result["source"])
        code=int(out)
        if code==1:return "updated"
        if code==0:return "stale"
        if code==-1:return "conflict"
        if code==2:return "duplicate"
        raise RuntimeError("unexpected Redis cache result")

    async def get(self,task_id,source_id,source,expected_revision):
        """Read only the current revision, supplied by the authoritative readback."""
        latest,snap=self.keys(task_id,source_id,source,expected_revision)
        raw=await self.redis.eval(_READ_LUA,2,latest,snap,str(expected_revision))
        if raw is None: return None
        value=json.loads(raw)
        expected={"environment":"shadow","run_id":self.run_id,"task_id":task_id,
                  "source_id":source_id,"source":source,"revision":str(expected_revision),
                  "source_revision":str(expected_revision)}
        if any(value.get(k)!=v for k,v in expected.items()) or value.get("result_hash")!=_hash_result(value):
            raise CacheContractError("cached_identity_or_hash")
        return value
