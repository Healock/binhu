"""Revision fenced Redis result cache for isolated shadow derived consumers."""
from __future__ import annotations
import hashlib, json, math, re
from datetime import datetime

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
  return 2
end
redis.call('HSET',KEYS[1],'revision',incoming,'result_hash',ARGV[3],'payload',ARGV[4])
redis.call('HSET',KEYS[2],'revision',incoming,'result_hash',ARGV[3],'payload',ARGV[4])
redis.call('EXPIRE',KEYS[2],ARGV[2])
return 1"""

def _validate_timestamp(v):
    if not isinstance(v,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",v): raise CacheContractError("generated_at")
    try: datetime.fromisoformat(v.replace("Z","+00:00"))
    except ValueError: raise CacheContractError("generated_at")

def _scalar(v,k):
    if isinstance(v,bool) or not isinstance(v,(str,int,float)) or (isinstance(v,float) and not math.isfinite(v)): raise CacheContractError(k)

def _hash_result(r):
    body={"task_id":r["task_id"],"source_id":r["source_id"],"revision":str(r["revision"]),"source_revision":str(r["source_revision"]),"source":r["source"],"content_hash":r["content_hash"],"fields":r["fields"]}
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

class RevisionCache:
    def __init__(self,redis,run_id,ttl_seconds=86400):
        if not _RUN.fullmatch(run_id) or type(ttl_seconds) is not int or not 60<=ttl_seconds<=604800: raise CacheContractError("scope")
        self.redis,self.run_id,self.ttl=redis,run_id,ttl_seconds
    async def put(self,result):
        if not isinstance(result,dict) or set(result)-{"task_id","source_id","revision","source_revision","generated_at","source","content_hash","fields"}: raise CacheContractError("envelope")
        if type(result.get("revision")) is not int or not 0<=result["revision"]<2**63 or type(result.get("source_revision")) is not int or result["source_revision"]!=result["revision"]: raise CacheContractError("revision")
        if type(result.get("source_id")) is not int or result["source_id"]<=0: raise CacheContractError("source_id")
        if result.get("source") not in _SOURCES: raise CacheContractError("source")
        for k in ("task_id","source"): _scalar(result.get(k),k)
        if "}" in result["task_id"] or not result["task_id"]: raise CacheContractError("task_id")
        _validate_timestamp(result.get("generated_at"))
        if not isinstance(result.get("content_hash"),str) or not _HASH.fullmatch(result["content_hash"]): raise CacheContractError("content_hash")
        fields=result.get("fields")
        if not isinstance(fields,dict) or set(fields)-_FIELDS: raise CacheContractError("fields")
        for k,v in fields.items(): _scalar(v,k)
        result_hash=_hash_result(result)
        payload={"environment":"shadow","run_id":self.run_id,"task_id":result["task_id"],"source_id":result["source_id"],"revision":str(result["revision"]),"source_revision":str(result["source_revision"]),"generated_at":result["generated_at"],"source":result["source"],"content_hash":result["content_hash"],"fields":fields,"result_hash":result_hash}
        raw=json.dumps(payload,ensure_ascii=False,separators=(",",":"),allow_nan=False)
        if len(raw.encode())>65536: raise CacheContractError("size")
        tag="{binhu:shadow:%s:derived:%s:%s}"%(self.run_id,result["task_id"],result["source_id"])
        latest=tag+":latest"; snap=tag+":revision:"+str(result["revision"])
        out=await self.redis.eval(_LUA,2,latest,snap,str(result["revision"]),str(self.ttl),result_hash,raw)
        code=int(out)
        if code==1:return "updated"
        if code==0:return "stale"
        if code==-1:return "conflict"
        if code==2:return "duplicate"
        raise RuntimeError("unexpected Redis cache result")
