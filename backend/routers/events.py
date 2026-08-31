"""Server-sent event gateway backed by the authoritative Redis Stream."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from config import settings
from deps import get_current_user


router = APIRouter(prefix="/api/events", tags=["实时事件"])
REDIS_RETRY_ERRORS = (redis.ConnectionError, redis.TimeoutError, OSError)


def _audience_allowed(audiences: list[str], user: dict) -> bool:
    permissions = set(user.get("permissions") or [])
    if "authenticated" in audiences:
        return True
    if "super_admin" in audiences and (user.get("role") == "super_admin" or user.get("is_super_admin")):
        return True
    user_id = str(user.get("id") or "")
    if f"user:{user_id}" in audiences:
        return True
    if any(item.startswith("permission:") and item.split(":", 1)[1] in permissions for item in audiences):
        return True
    community_id = user.get("community_id") or (user.get("community") or {}).get("id")
    community_names = {str(item) for item in (user.get("community_names") or []) if item}
    return any(
        item.startswith("community:")
        and (item.split(":", 1)[1] == str(community_id) or item.split(":", 1)[1] in community_names)
        for item in audiences
    )


def _parse_stream(entries):
    for stream_id, fields in entries or []:
        raw = fields.get("event") if isinstance(fields, dict) else None
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        event["stream_id"] = str(stream_id)
        yield event


async def _event_generator(request: Request, user: dict) -> AsyncIterator[str]:
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    seen: set[str] = set()
    try:
        last_id = request.headers.get("last-event-id") or ""
        # Redis may be temporarily unavailable when the browser first opens
        # the stream.  Keep the SSE connection alive and report a recoverable
        # state instead of raising before the read loop has started (which
        # otherwise surfaces as an HTTP 500 response).
        while not last_id and not await request.is_disconnected():
            try:
                latest = await client.xrevrange(settings.REDIS_STREAM_KEY, count=1)
                last_id = str(latest[0][0]) if latest else "0-0"
            except REDIS_RETRY_ERRORS:
                yield "event: realtime_unavailable\ndata: {}\n\n"
                await asyncio.sleep(3)
        if last_id and request.headers.get("last-event-id"):
            try:
                first = await client.xrange(settings.REDIS_STREAM_KEY, min=last_id, max=last_id, count=1)
                if not first and last_id != "0-0":
                    yield "event: resync_required\ndata: {}\n\n"
                    latest = await client.xrevrange(settings.REDIS_STREAM_KEY, count=1)
                    last_id = str(latest[0][0]) if latest else "0-0"
            except REDIS_RETRY_ERRORS:
                yield "event: realtime_unavailable\ndata: {}\n\n"
                await asyncio.sleep(3)
        while not await request.is_disconnected():
            try:
                result = await client.xread({settings.REDIS_STREAM_KEY: last_id}, count=100, block=15000)
                emitted = False
                for event in _parse_stream(result[0][1] if result else []):
                    stream_id = event.get("stream_id")
                    last_id = str(stream_id)
                    event_id = str(event.get("event_id") or stream_id)
                    if event_id in seen:
                        continue
                    seen.add(event_id)
                    if len(seen) > 50000:
                        seen = set(list(seen)[-25000:])
                    if not _audience_allowed(event.get("audiences") or [], user):
                        continue
                    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {stream_id}\nevent: domain_event\ndata: {body}\n\n"
                    emitted = True
                if not emitted:
                    yield ": keep-alive\n\n"
            except REDIS_RETRY_ERRORS:
                yield "event: realtime_unavailable\ndata: {}\n\n"
                await asyncio.sleep(3)
    finally:
        await client.aclose()


@router.get("/stream")
async def event_stream(request: Request, user: dict = Depends(get_current_user)):
    if not settings.REALTIME_EVENTS_ENABLED:
        raise HTTPException(status_code=503, detail="实时事件暂未启用")
    return StreamingResponse(
        _event_generator(request, user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
