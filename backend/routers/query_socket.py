"""Authenticated spreadsheet edits and version notifications, without Redis."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
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


def parse_edit(raw: str) -> EditMessage:
    if len(raw.encode('utf-8')) > 65536:
        raise ValueError('message too large')
    return EditMessage.model_validate_json(raw)


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
                edit = parse_edit(raw)
            except (ValueError, ValidationError):
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
        _connections[key] -= 1
        if not _connections[key]:
            del _connections[key]
