import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
os.environ.setdefault('ENCRYPTION_KEY', 'test-key')

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from routers import query_socket


class QuerySocketTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(query_socket.router)
        self.client = TestClient(app)
        self.user = {'id': 7, 'role': 'admin', 'permissions': ['online.raw.view', 'online.raw.edit']}
        self.headers = {'origin': 'http://testserver', 'cookie': 'binhu_session=synthetic-session'}

    def test_foreign_origin_is_rejected_before_database(self):
        with patch.object(query_socket, 'get_current_user', new_callable=AsyncMock) as auth:
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('/api/query/live/全链条', headers={'origin': 'https://other.invalid'}):
                    pass
            auth.assert_not_awaited()

    def test_live_version_and_edit_use_existing_transaction_and_revision(self):
        saved = {
            'revision': 6,
            'row_key': 'r',
            'values': {'核查结果': '离苏', '备注': '其他用户内容'},
            'changed_values': {'核查结果': '离苏'},
            'data_version': '12:42:2026-09-12T08:00:00',
            'pending_sync': False,
        }
        with patch.object(query_socket, 'get_current_user', new=AsyncMock(return_value=self.user)), \
             patch.object(query_socket, 'read_version', new=AsyncMock(return_value='v1')), \
             patch.object(query_socket, 'save_cell', new=AsyncMock(return_value=saved)) as save:
            with self.client.websocket_connect('/api/query/live/全链条', headers=self.headers) as ws:
                self.assertEqual(ws.receive_json(), {'type': 'version', 'data_version': 'v1'})
                ws.send_json({'type': 'edit', 'request_id': 'a1', 'source_id': 12,
                              'column': '核查结果', 'value': '离苏', 'expected_revision': 5, 'expected_row_hash': 'h'})
                response = ws.receive_json()
                self.assertEqual(response['type'], 'saved')
                self.assertEqual(response['result']['revision'], 6)
                self.assertEqual(response['result']['changed_values'], {'核查结果': '离苏'})
                self.assertEqual(response['result']['data_version'], '12:42:2026-09-12T08:00:00')
                self.assertEqual(save.await_args.args[1], 12)
                self.assertEqual(save.await_args.args[2].expected_revision, 5)
                # The local save response carries the version read from the
                # same transaction; no second version query is needed.
                self.assertEqual(query_socket.read_version.await_count, 1)

    def test_flow_member_can_read_version_but_query_edit_is_rejected(self):
        member = {
            'id': 8,
            'role': 'member',
            'member': {'id': 18, 'position': '组员'},
            'permissions': ['online.raw.view', 'online.raw.edit'],
            'permission_groups': [{'code': 'flow_post'}],
        }
        with patch.object(query_socket, 'get_current_user', new=AsyncMock(return_value=member)), \
             patch.object(query_socket, 'read_version', new=AsyncMock(return_value='v1')), \
             patch.object(query_socket, 'save_cell', new_callable=AsyncMock) as save:
            with self.client.websocket_connect('/api/query/live/全链条', headers=self.headers) as ws:
                self.assertEqual(ws.receive_json(), {'type': 'version', 'data_version': 'v1'})
                ws.send_json({'type': 'edit', 'request_id': 'member-edit', 'source_id': 1,
                              'column': '核查结果', 'value': '离苏', 'expected_revision': 1,
                              'expected_row_hash': 'h'})
                response = ws.receive_json()
                self.assertEqual(response['type'], 'error')
                self.assertEqual(response['status'], 403)
                save.assert_not_awaited()

    def test_revision_conflict_is_preserved_and_no_retry(self):
        with patch.object(query_socket, 'get_current_user', new=AsyncMock(return_value=self.user)), \
             patch.object(query_socket, 'read_version', new=AsyncMock(return_value='v1')), \
             patch.object(query_socket, 'save_cell', new=AsyncMock(side_effect=HTTPException(409, '版本冲突'))) as save:
            with self.client.websocket_connect('/api/query/live/全链条', headers=self.headers) as ws:
                ws.receive_json()
                ws.send_json({'type': 'edit', 'request_id': 'a2', 'source_id': 1,
                              'column': '核查结果', 'value': '离苏', 'expected_revision': 5, 'expected_row_hash': 'h'})
                self.assertEqual(ws.receive_json()['status'], 409)
                self.assertEqual(save.await_count, 1)

    def test_permission_revocation_stops_edits(self):
        with patch.object(query_socket, 'get_current_user', new=AsyncMock(side_effect=[self.user, HTTPException(401, 'expired')])), \
             patch.object(query_socket, 'read_version', new=AsyncMock(return_value='v1')), \
             patch.object(query_socket, 'save_cell', new_callable=AsyncMock) as save:
            with self.client.websocket_connect('/api/query/live/全链条', headers=self.headers) as ws:
                ws.receive_json()
                ws.send_json({'type': 'edit', 'request_id': 'a3', 'source_id': 1,
                              'column': '核查结果', 'value': '离苏', 'expected_revision': 5, 'expected_row_hash': 'h'})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
                save.assert_not_awaited()

    def test_explicit_desktop_origin_accepted_without_wildcards(self):
        with patch.object(query_socket, 'settings', SimpleNamespace(cors_allowed_origins=['binhu://app', '*'])):
            for origin, expected in [('binhu://app', True), ('null', False), ('https://other.invalid', False)]:
                socket = SimpleNamespace(headers={'origin': origin, 'host': 'testserver'}, url=SimpleNamespace(scheme='ws'))
                with self.subTest(origin=origin):
                    self.assertEqual(query_socket.origin_allowed(socket), expected)

    def test_duplicate_request_id_cannot_repeat_write(self):
        with patch.object(query_socket, 'get_current_user', new=AsyncMock(return_value=self.user)), \
             patch.object(query_socket, 'read_version', new=AsyncMock(return_value='v1')), \
             patch.object(query_socket, 'save_cell', new=AsyncMock(return_value={'revision': 2})) as save:
            with self.client.websocket_connect('/api/query/live/全链条', headers=self.headers) as ws:
                ws.receive_json()
                payload = {'type': 'edit', 'request_id': 'same', 'source_id': 1,
                           'column': '核查结果', 'value': '离苏', 'expected_revision': 1, 'expected_row_hash': 'h'}
                ws.send_json(payload)
                self.assertEqual(ws.receive_json()['type'], 'saved')
                ws.send_json(payload)
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
                self.assertEqual(save.await_count, 1)

    def test_schema_rejects_extra_fields_and_oversize(self):
        with self.assertRaises(ValueError):
            query_socket.parse_edit(json.dumps({'type': 'edit', 'request_id': 'r', 'source_id': 1,
                'column': 'x', 'value': 'y', 'expected_revision': 1, 'sql': 'DROP'}))
        with self.assertRaises(ValueError):
            query_socket.parse_edit('x' * 70000)
