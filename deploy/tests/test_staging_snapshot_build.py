import json
import copy
import hashlib
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from services.parsers import get_parser
from deploy.environments.staging_data.build import build
from deploy.environments.staging_data.codec import SnapshotError
from deploy.environments.staging_data.registry import FIELDS
from deploy.environments.staging_data.organization import FIELDS as ORG_FIELDS
from deploy.environments.staging_data.tasks import TASK_TYPES


class Cursor:
    def __init__(self, tables):
        self.tables = tables
        self.commands = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def execute(self, sql, params=()):
        self.commands.append(sql)
        if sql.startswith('SELECT'):
            match = re.search(r'FROM `([^`]+)`\.`([^`]+)`', sql)
            table = '.'.join(match.groups())
            columns = re.findall(r'`([^`]+)`', sql.split(' FROM ')[0])
            rows = self.tables[table]
            if 'spreadsheet_id<>0' in sql:
                rows = [row for row in rows if row.get('spreadsheet_id', 0) != 0
                        and row.get('parser_type') == params[0]]
            self.result = [tuple(row[c] for c in columns) for row in rows]
    async def fetchall(self):
        return self.result


class BuildTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        settings = SimpleNamespace(APP_ENVIRONMENT='production', MYSQL_DOMAIN_DATABASES_ENABLED=True,
            PLATFORM_DOMAIN_ACTIVE=True, REGISTRY_ADDRESS_DOMAIN_ACTIVE=True,
            MYSQL_ONLINE_DATA_DB='OnlineData', MYSQL_PLATFORM_DB='PlatformData', MYSQL_REGISTRY_DB='RegistryData',
            MYSQL_ARCHIVE_DB='OnlineDataArchive')
        tables = {name: [] for name in [*FIELDS, *ORG_FIELDS]}
        tables['PlatformData._areas'] = [{'id': 1, 'name': '虚构片区'}]
        tables['PlatformData._communities'] = [{'id': 1, 'name': '虚构社区', 'area_id': 1, 'is_active': 1}]
        tables['PlatformData._community_aliases'] = []
        for kind in TASK_TYPES:
            tables['OnlineData.' + get_parser(kind).table_name] = []
            tables['OnlineDataArchive.' + get_parser(kind).table_name + '_archive'] = []
        tables['OnlineData._local_source_records'] = []
        parser = get_parser('全链条')
        values = {field: '' for field in parser.COLUMNS}
        values.update({'社区': '虚构社区', '姓名': '虚构甲', '身份证号': 'fictional-id', '地址': '虚构路', '核查结果': '待登记'})
        tables['OnlineData.t_fullchain'] = [{'id': 1, '_row_key': 'old'}]
        tables['OnlineData._online_source_rows'] = [{'id': 2, 'parser_type': '全链条', 'physical_row': 1,
            'revision': 5, 'row_key': 'old', 'row_hash': 'a'*64, 'values_json': json.dumps(values), 'source_kind': 'local_table'}]
        tables['OnlineData._unverifiable_review_flows'] = []
        tables['OnlineData._task_registration_links'] = []
        tables['OnlineData._online_task_address_matches'] = []
        tables['OnlineData._unverifiable_review_events'] = []
        tables['OnlineData._task_registration_events'] = []
        cur = Cursor(tables)
        conn = SimpleNamespace(cursor=lambda: cur, rollback=AsyncMock())
        return settings, tables, cur, conn

    async def test_source_identity_gate_precedes_all_queries(self):
        settings, _, cur, conn = self.fixture()
        settings.APP_ENVIRONMENT = 'staging'
        with self.assertRaises(SnapshotError):
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        self.assertEqual(cur.commands, [])

    async def test_archive_database_identity_is_checked_before_queries(self):
        settings, _, cur, conn = self.fixture()
        settings.MYSQL_ARCHIVE_DB = 'Staging_OnlineDataArchive'
        with self.assertRaisesRegex(SnapshotError, 'source_database_name_mismatch'):
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        self.assertEqual(cur.commands, [])

    async def test_consistent_readonly_transaction_and_safe_source_hash(self):
        settings, _, cur, conn = self.fixture()
        result = await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        self.assertIn('READ ONLY', cur.commands[1])
        self.assertTrue(all(sql.startswith(('SELECT', 'SET SESSION', 'START TRANSACTION')) for sql in cur.commands))
        conn.rollback.assert_awaited_once()
        self.assertEqual(result['report']['current_task_count'], 1)
        self.assertFalse(result['report']['ready_for_application_switch'])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('fictional-id', encoded)
        self.assertNotIn('虚构路', encoded)

    async def test_mismatched_business_table_aborts_and_rolls_back(self):
        settings, tables, _, conn = self.fixture()
        tables['OnlineData.t_fullchain'] = []
        with self.assertRaisesRegex(SnapshotError, '^business_source_count_or_key_mismatch$'):
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        conn.rollback.assert_awaited_once()

    async def test_active_record_is_not_excluded_by_an_old_archive_with_same_key(self):
        settings, tables, _, conn = self.fixture()
        tables['OnlineData._online_source_rows'] = []
        tables['OnlineData._local_source_records'] = [
            {'local_task_id': 1, 'business_key': 'old', 'status': 'active'}]
        tables['OnlineDataArchive.t_fullchain_archive'] = [{'_row_key': 'old'}]
        with self.assertRaisesRegex(SnapshotError, 'business_source_count_or_key_mismatch') as caught:
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        self.assertEqual(caught.exception.diagnostics['business_only_active_ledger_count'], 1)
        self.assertEqual(caught.exception.diagnostics['business_only_archive_key_count'], 1)
        conn.rollback.assert_awaited_once()

    async def test_supported_local_origins_and_rejection_of_external_sources(self):
        for kind in ('local_table','local_dispatch','one_time_continuation_import','txdocs','local_unknown'):
            settings,tables,_,conn=self.fixture()
            tables['OnlineData._online_source_rows'][0]['source_kind']=kind
            if kind in ('txdocs','local_unknown'):
                with self.assertRaisesRegex(SnapshotError,'^unsupported_current_source$'):
                    await build(conn,'staging-'+'a'*16,b'a'*32,settings=settings)
            else:
                result=await build(conn,'staging-'+'a'*16,b'a'*32,settings=settings)
                self.assertEqual(result['report']['current_task_count'],1)
                self.assertEqual(result['tables']['OnlineData._online_source_rows'][0]['source_kind'],kind)
                self.assertEqual(result['tables']['OnlineData._local_source_records'][0]['source_kind'],kind)

    def recovery_fixture(self):
        settings, tables, cur, conn = self.fixture()
        parser = get_parser('疑似未注销模型三')
        values = {field: '' for field in parser.COLUMNS}
        values.update({'社区': '虚构社区', '下发社区': '虚构社区', '姓名': '虚构乙', '身份证号': 'synthetic-id'})
        key = parser.make_row_key(values)
        values = {field: str(values.get(field, '') or '') for field in parser.COLUMNS}
        raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        tables['OnlineData.' + parser.table_name] = [{'id': 9, '_row_key': key, **values}]
        tables['OnlineData._local_source_records'] = [{'parser_type': parser.parser_type,
            'local_task_id': 9, 'business_key': key, 'status': 'active', 'archived_at': None,
            'source_kind': 'local_table', 'source_ref': parser.table_name + ':9',
            'values_json': raw, 'content_hash': hashlib.sha256(raw.encode()).hexdigest(), 'revision': 7}]
        return settings, tables, cur, conn

    async def test_recovery_requires_opt_in_and_retains_ledger_revision(self):
        settings, tables, cur, conn = self.recovery_fixture()
        before = copy.deepcopy(tables)
        with self.assertRaisesRegex(SnapshotError, 'business_source_count_or_key_mismatch'):
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings)
        result = await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings,
            recover_model_three_sources=True)
        recovered = [r for r in result['tables']['OnlineData._online_source_rows']
                     if r['parser_type'] == '疑似未注销模型三']
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['revision'], 7)
        self.assertEqual(result['report']['recovered_model_three_source_count'], 1)
        self.assertEqual(result['report']['selected_source_counts']['OnlineData._online_source_rows'], 1)
        self.assertEqual(tables, before)
        self.assertTrue(all(sql.startswith(('SELECT', 'SET SESSION', 'START TRANSACTION')) for sql in cur.commands))

    async def test_recovery_rejects_unproven_or_duplicate_ledgers(self):
        for field, value in [('status','archived'), ('content_hash','f'*64), ('revision',0),
                ('business_key','bad'), ('values_json','{}'), ('source_kind','txdocs'),
                ('archived_at','2026-09-01'), ('local_task_id',10)]:
            with self.subTest(field=field):
                settings, tables, _, conn = self.recovery_fixture()
                tables['OnlineData._local_source_records'][0][field] = value
                with self.assertRaises(SnapshotError):
                    await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings,
                        recover_model_three_sources=True)
        settings, tables, _, conn = self.recovery_fixture()
        tables['OnlineData._local_source_records'] *= 2
        with self.assertRaises(SnapshotError):
            await build(conn, 'staging-'+'a'*16, b'a'*32, settings=settings,
                recover_model_three_sources=True)


if __name__ == '__main__':
    unittest.main()

