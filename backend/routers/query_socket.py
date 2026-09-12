"""Authenticated spreadsheet edits and version notifications, without Redis."""
from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict, deque
from itertools import count
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from config import settings
from database import db_manager
from deps import get_current_user
from services.permissions import (
    ONLINE_RAW_VIEW,
    can_edit_online_query,
    has_permission,
)
from routers.query import CellUpdate, QUERY_TYPES, query_data_version, update_source_cell

router = APIRouter(prefix='/api/query', tags=['在线工作表实时协作'])
_connections: Counter = Counter()
_subscribers: dict[str, set[WebSocket]] = defaultdict(set)
_events: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=1000))
_event_ids = count(1)
MAX_CONNECTIONS_PER_USER = 3
POLL_SECONDS = 5


class EditMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['edit']
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    source_id: int = Field(gt=0)
    column: str = Field(min_length=1, max_length=200)
    value: str = Field(default='', max_length=10000)
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)
    explicit_text_edit: bool = False


class ResumeMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['resume']
    after_event_id: int = Field(ge=0)
    data_version: str = Field(default='', max_length=128)


class SelectionPresenceMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['selection_presence']
    row_key: str = Field(default='', max_length=255)
    start_row: int = Field(ge=0, le=1_000_000)
    start_column: int = Field(ge=0, le=10_000)
    end_row: int = Field(ge=0, le=1_000_000)
    end_column: int = Field(ge=0, le=10_000)
    mode: Literal['viewing', 'editing'] = 'viewing'


def parse_edit(raw: str) -> EditMessage:
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('message too large')
    return EditMessage.model_validate_json(raw)


def _safe_display_name(user: dict) -> str:
    member = user.get('member') or {}
    return str(member.get('name') or user.get('display_name') or '协作者')[:64]


async def _broadcast(parser_type: str, payload: dict, *, exclude: WebSocket | None = None) -> None:
    _events[parser_type].append(payload)
    stale: list[WebSocket] = []
    for subscriber in tuple(_subscribers[parser_type]):
        if subscriber is exclude:
            continue
        try:
            await subscriber.send_json(payload)
        except Exception:
            stale.append(subscriber)
    for subscriber in stale:
        _subscribers[parser_type].discard(subscriber)


def _events_after(parser_type: str, event_id: int) -> list[dict] | None:
    events = list(_events[parser_type])
    if not events:
        return []
    if event_id >= events[-1]['event_id']:
        return []
    if event_id < events[0]['event_id'] - 1:
        return None
    return [event for event in events if event['event_id'] > event_id]


def _request(socket: WebSocket, *, active: bool = False) -> Request:
    scope = dict(socket.scope)
    scope.update(type='http', method='PATCH' if active else 'GET')
    scope['headers'] = [(key, value) for key, value in scope['headers'] if key.lower() != b'x-user-activity']
    scope['headers'].append((b'x-user-activity', b'1' if active else b'0'))
    return Request(scope)


def origin_allowed(socket: WebSocket) -> bool:
    origin = socket.headers.get('origin', '')
    if not origin or origin in {'null', '*'}:
        return False
    # Desktop shells use an explicit non-HTTP origin already trusted by CORS.
    if origin in settings.cors_allowed_origins:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {'http', 'https'} or parsed.path not in {'', '/'}:
        return False
    scheme = 'https' if socket.url.scheme == 'wss' else 'http'
    return origin.rstrip('/') == f'{scheme}://{socket.headers.get("host", "")}'


async def read_version(parser_type, user):
    pool = db_manager.get_pool('online_data')
    conn = await asyncio.wait_for(pool.acquire(), timeout=5)
    try:
        return (await query_data_version(parser_type, user=user, conn=conn))['data_version']
    finally:
        pool.release(conn)


async def save_cell(parser_type, source_id, data, request, user):
    pool = db_manager.get_pool('online_data')
    conn = await asyncio.wait_for(pool.acquire(), timeout=5)
    try:
        return await update_source_cell(parser_type, source_id, data, request, user=user, conn=conn)
    finally:
        pool.release(conn)


async def _authorize(socket, *, active=False):
    user = await asyncio.wait_for(get_current_user(_request(socket, active=active)), timeout=5)
    if not has_permission(user, ONLINE_RAW_VIEW):
        raise HTTPException(403, '无在线查询权限')
    return user


@router.websocket('/live/{parser_type}')
async def spreadsheet_socket(socket: WebSocket, parser_type: str):
    if parser_type not in QUERY_TYPES or not origin_allowed(socket):
        await socket.close(code=1008)
        return
    try:
        user = await _authorize(socket)
    except HTTPException:
        await socket.close(code=1008)
        return
    key = (settings.APP_ENVIRONMENT, int(user['id']))
    if _connections[key] >= MAX_CONNECTIONS_PER_USER:
        await socket.close(code=1013)
        return
    _connections[key] += 1
    seen: set[str] = set()
    try:
        await socket.accept()
        _subscribers[parser_type].add(socket)
        version = await read_version(parser_type, user)
        await socket.send_json({'type': 'version', 'data_version': version})
        while True:
            try:
                raw = await asyncio.wait_for(socket.receive_text(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                user = await _authorize(socket)
                current = await read_version(parser_type, user)
                if current != version:
                    version = current
                    await socket.send_json({'type': 'version', 'data_version': version})
                else:
                    await socket.send_json({'type': 'heartbeat'})
                continue
            user = await _authorize(socket, active=True)
            try:
                envelope = json.loads(raw)
                message_type = envelope.get('type') if isinstance(envelope, dict) else None
                if message_type == 'resume':
                    resume = ResumeMessage.model_validate(envelope)
                    replay = _events_after(parser_type, resume.after_event_id)
                    if replay is None:
                        await socket.send_json({'type': 'resync_required', 'data_version': version})
                    else:
                        for event in replay:
                            await socket.send_json(event)
                    continue
                if message_type == 'selection_presence':
                    presence = SelectionPresenceMessage.model_validate(envelope)
                    await _broadcast(parser_type, {
                        'type': 'selection_presence',
                        'parser_type': parser_type,
                        'row_key': presence.row_key,
                        'start_row': presence.start_row,
                        'start_column': presence.start_column,
                        'end_row': presence.end_row,
                        'end_column': presence.end_column,
                        'mode': presence.mode,
                        'user_id': int(user['id']),
                        'display_name': _safe_display_name(user),
                    }, exclude=socket)
                    continue
                edit = EditMessage.model_validate(envelope)
            except (ValueError, ValidationError, json.JSONDecodeError):
                await socket.close(code=1008)
                return
            if edit.request_id in seen or len(seen) >= 10000:
                await socket.close(code=1008)
                return
            seen.add(edit.request_id)
            try:
                if not can_edit_online_query(user):
                    raise HTTPException(403, '无在线编辑权限')
                data = CellUpdate(**edit.model_dump(exclude={'type', 'request_id', 'source_id'}))
                result = await save_cell(parser_type, edit.source_id, data, _request(socket, active=True), user)
                await socket.send_json({'type': 'saved', 'request_id': edit.request_id, 'result': result})
                # Local saves already calculate the authoritative version on
                # the same transaction/connection.  Reusing it avoids taking
                # a second online-data connection while the save request is
                # still being handled.  The fallback is retained only for
                # legacy compatibility responses which predate data_version.
                current_version = str(result.get('data_version') or '')
                if not current_version:
                    current_version = await read_version(parser_type, user)
                version = current_version
                event = {
                    'type': 'row_changed',
                    'event_id': next(_event_ids),
                    'data_version': current_version,
                    'parser_type': parser_type,
                    'source_id': edit.source_id,
                    'row_key': str(result.get('row_key') or ''),
                    'revision': int(result.get('revision') or 0),
                    'row_hash': str(result.get('row_hash') or ''),
                    'changed_fields': {edit.column: str((result.get('values') or {}).get(edit.column, ''))},
                    'changed_by': {'user_id': int(user['id']), 'display_name': _safe_display_name(user)},
                }
                await _broadcast(parser_type, event, exclude=socket)
            except HTTPException as exc:
                await socket.send_json({'type': 'error', 'request_id': edit.request_id,
                                        'status': exc.status_code, 'detail': exc.detail})
            except Exception:
                # A transport error after commit has an unknown outcome. Never replay.
                await socket.send_json({'type': 'error', 'request_id': edit.request_id,
                                        'status': 503, 'detail': '保存结果待核对，请重新读取该行后再操作'})
    except HTTPException:
        await socket.close(code=1008)
    except WebSocketDisconnect:
        pass
    except Exception:
        await socket.close(code=1011)
    finally:
        _subscribers[parser_type].discard(socket)
        _connections[key] -= 1
        if not _connections[key]:
            del _connections[key]
