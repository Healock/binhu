"""Loopback-only HTTP/SSE/WebSocket checks against guarded synthetic MySQL."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from verify import guard, require


async def main():
    results = {'run_id': os.environ.get('LOAD_TEST_RUN_ID'), 'checks': []}
    server = None
    serving = None
    initialized = False
    try:
        await guard()
        from database import init_db, close_db, db_manager
        from routers import auth, query, query_socket, admin_ops, users
        from services.local_source import mirror_business_tables_to_local_sources
        from services.parsers import get_parser
        from services.online_summary_updates import stop_online_summary_update_processing
        await init_db()
        initialized = True
        parser = get_parser('疑似返苏')
        nonce = uuid4().hex[:12]
        values = {c: '' for c in parser.COLUMNS}
        values.update({'姓名': '合成验收-' + nonce, '身份证号码': 'SYNTHETIC-' + nonce,
                       '联系号码': 'TEST-' + nonce, '社区': '', '核查反馈': '无需登记'})
        row_key = parser.make_row_key(values)
        pool = db_manager.get_pool('online_data')
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    'INSERT INTO t_suspect_return (_row_key,' + ','.join(f'`{c}`' for c in parser.COLUMNS)
                    + ') VALUES (' + ','.join(['%s'] * (len(parser.COLUMNS) + 1)) + ')',
                    [row_key, *[values[c] for c in parser.COLUMNS]],
                )
            await mirror_business_tables_to_local_sources(conn)
            await conn.commit()
            async with conn.cursor() as cur:
                await cur.execute('SELECT id,revision,row_hash FROM _online_source_rows '
                                  'WHERE parser_type=%s AND row_key=%s AND archived_at IS NULL',
                                  ('疑似返苏', row_key))
                source_id, revision, row_hash = await cur.fetchone()

        # Exercise production routers and authentication, but never start main's schedulers.
        app = FastAPI()
        for router in (auth.router, query.router, query_socket.router, admin_ops.router, users.router):
            app.include_router(router)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18788,
                                              access_log=False, log_level='error', lifespan='off'))
        serving = asyncio.create_task(server.serve())
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.05)
        base = 'http://127.0.0.1:18788'
        async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
            require((await client.get('/api/admin/ops/audit/stream')).status_code == 401,
                    'unauthenticated SSE was allowed')
            login = await client.post('/api/auth/login', json={
                'username': os.environ['BOOTSTRAP_ADMIN_USERNAME'],
                'password': os.environ['BOOTSTRAP_ADMIN_PASSWORD'],
            })
            require(login.status_code == 200, 'synthetic login failed')
            cookie = '; '.join(f'{k}={v}' for k, v in client.cookies.items())
            socket_url = 'ws://127.0.0.1:18788/api/query/live/' + quote('疑似返苏')
            try:
                async with connect(socket_url, origin='https://invalid.example',
                                   additional_headers={'Cookie': cookie}):
                    raise AssertionError('foreign origin accepted')
            except InvalidStatus as exc:
                require(exc.response.status_code == 403, 'foreign origin wrong status')
            async with connect(socket_url, origin=base, additional_headers={'Cookie': cookie}) as ws:
                require(json.loads(await asyncio.wait_for(ws.recv(), 10))['type'] == 'version',
                        'WebSocket initial version missing')
                edit = {'type': 'edit', 'request_id': 'real-' + nonce, 'source_id': source_id,
                        'column': '姓名', 'value': '合成验收修改-' + nonce,
                        'expected_revision': revision, 'expected_row_hash': row_hash}
                await ws.send(json.dumps(edit))
                response = json.loads(await asyncio.wait_for(ws.recv(), 15))
                require(response.get('type') == 'saved',
                        'WebSocket save failed: ' + str(response.get('status', 'unknown')))
                require(response['result']['revision'] == revision + 1, 'save revision did not advance')
                edit['request_id'] += '-stale'
                await ws.send(json.dumps(edit))
                response = json.loads(await asyncio.wait_for(ws.recv(), 15))
                require(response.get('status') == 409, 'stale WebSocket edit was accepted')
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute('SELECT revision FROM _online_source_rows WHERE id=%s', (source_id,))
                    require((await cur.fetchone())[0] == revision + 1, 'conflict repeated a write')
            results['checks'].append('real_websocket_login_origin_save_revision_conflict')

            # Even explicitly granting raw.edit must not let a regular member
            # bypass the query editor's separate position boundary.
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        'INSERT INTO _permission_groups (code,name,permissions,data_scope) '
                        'VALUES (%s,%s,%s,%s)',
                        ('shadow-' + nonce, 'Synthetic-' + nonce,
                         json.dumps(['online.raw.view', 'online.raw.edit']), 'all'),
                    )
                    group_id = int(cur.lastrowid)
                await conn.commit()
            password = secrets.token_urlsafe(24)
            username = 'member-' + nonce + '@shadow'
            created = await client.post('/api/users', json={
                'username': username, 'display_name': 'Synthetic member ' + nonce,
                'password': password, 'assignment_mode': 'custom',
                'permission_group_ids': [group_id], 'password_is_temporary': False,
            })
            require(created.status_code == 200, 'synthetic restricted account creation failed')
            async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as restricted:
                login = await restricted.post('/api/auth/login', json={
                    'username': username, 'password': password,
                })
                require(login.status_code == 200, 'restricted login failed')
                member_cookie = '; '.join(f'{k}={v}' for k, v in restricted.cookies.items())
                async with connect(socket_url, origin=base,
                                   additional_headers={'Cookie': member_cookie}) as ws:
                    require(json.loads(await asyncio.wait_for(ws.recv(), 10))['type'] == 'version',
                            'restricted account cannot read query version')
                    edit['request_id'] = 'restricted-' + nonce
                    edit['expected_revision'] = revision + 1
                    await ws.send(json.dumps(edit))
                    denied = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    require(denied.get('status') == 403, 'restricted WebSocket edit allowed')
                denied_http = await restricted.patch(
                    '/api/query/' + quote('疑似返苏') + '/source-rows/' + str(source_id),
                    json={k: v for k, v in edit.items()
                          if k not in {'type', 'request_id', 'source_id'}},
                )
                require(denied_http.status_code == 403, 'restricted HTTP edit allowed')
                require((await restricted.get('/api/admin/ops/audit/stream')).status_code == 403,
                        'restricted audit stream allowed')
                await restricted.post('/api/auth/logout')
            results['checks'].append('real_restricted_role_http_websocket_sse_denied')

            action = 'release.synthetic.' + nonce
            async def insert_audit():
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute('INSERT INTO _admin_audit_log '
                            '(username,action,target_type,target_name,result,detail_json) '
                            'VALUES (%s,%s,%s,%s,%s,%s)',
                            ('synthetic', action, 'test', 'PRIVATE-BODY-MARKER', 'success',
                             '{"password":"PRIVATE-BODY-MARKER"}'))
                        identifier = int(cur.lastrowid)
                    await conn.commit()
                return identifier
            first_id = await insert_audit()
            direct_rows = await admin_ops._read_audit_rows_after(first_id - 1, action)
            require(any(int(row[0]) == first_id for row in direct_rows), 'audit SQL missed committed row')
            async def receive_id(target, headers=None):
                async with client.stream('GET', '/api/admin/ops/audit/stream',
                                         params={'action': action, 'after_id': first_id - 1}, headers=headers) as response:
                    require(response.status_code == 200, 'SSE response rejected')
                    require(response.headers.get('x-accel-buffering') == 'no', 'SSE buffering enabled')
                    async with asyncio.timeout(15):
                        async for line in response.aiter_lines():
                            require(line != 'event: audit_unavailable', 'SSE audit query unavailable')
                            require(line != 'event: auth_revoked', 'SSE session revoked unexpectedly')
                            require('PRIVATE-BODY-MARKER' not in line, 'SSE leaked raw body')
                            if line.startswith('data: '):
                                data = json.loads(line[6:])
                                if data.get('id') == target:
                                    return
                    raise AssertionError('SSE event absent')
            await receive_id(first_id)
            second_id = await insert_audit()
            await receive_id(second_id, {'Last-Event-ID': str(first_id)})
            results['checks'].append('real_sse_auth_delivery_redaction_last_event_id_resume')
            await client.post('/api/auth/logout')
            require((await client.get('/api/admin/ops/audit/stream')).status_code == 401,
                    'logged-out SSE still authenticated')
            results['checks'].append('real_session_revocation')
        results['status'] = 'passed'
    except BaseException as exc:
        results.update(status='failed', error_type=type(exc).__name__, error_message=str(exc)[:180])
        raise
    finally:
        if server:
            server.should_exit = True
        if serving:
            await asyncio.wait_for(serving, 15)
        if initialized:
            await stop_online_summary_update_processing()
            await close_db()
        Path('/artifacts/realtime.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == '__main__':
    asyncio.run(main())
