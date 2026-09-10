import json
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
            self.result = [tuple(row[c] for c in columns) for row in self.tables[table]]
    async def fetchall(self):
        return self.result


class BuildTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        settings = SimpleNamespace(APP_ENVIRONMENT='production', MYSQL_DOMAIN_DATABASES_ENABLED=True,
            PLATFORM_DOMAIN_ACTIVE=True, REGISTRY_ADDRESS_DOMAIN_ACTIVE=True,
            MYSQL_ONLINE_DATA_DB='OnlineData', MYSQL_PLATFORM_DB='PlatformData', MYSQL_REGISTRY_DB='RegistryData')
        tables = {name: [] for name in [*FIELDS, *ORG_FIELDS]}
        tables['PlatformData._areas'] = [{'id': 1, 'name': '虚构片区'}]
        tables['PlatformData._communities'] = [{'id': 1, 'name': '虚构社区', 'area_id': 1, 'is_active': 1}]
        tables['PlatformData._community_aliases'] = []
        for kind in TASK_TYPES:
            tables['OnlineData.' + get_parser(kind).table_name] = []
        parser = get_parser('全链条')
        values = {field: '' for field in parser.COLUMNS}
        values.update({'社区': '虚构社区', '姓名': '虚构甲', '身份证号': 'fictional-id', '地址': '虚构路', '核查结果': '待登记'})
        tables['OnlineData.t_fullchain'] = [{'id': 1, '_row_key': 'old'}]
        tables['OnlineData._online_source_rows'] = [{'id': 2, 'parser_type': '全链条', 'physical_row': 1,
            'revision': 5, 'row_key': 'old', 'row_hash': 'a'*64, 'values_json': json.dumps(values), 'source_kind': 'local_table'}]
        tables['OnlineData._unverifiable_review_flows'] = []
        tables['OnlineData._task_registration_links'] = []
        cur = Cursor(tables)
        conn = SimpleNamespace(cursor=lambda: cur, rollback=AsyncMock())
        return settings, tables, cur, conn

    async def test_source_identity_gate_precedes_all_queries(self):
        settings, _, cur, conn = self.fixture()
        settings.APP_ENVIRONMENT = 'staging'
        with self.assertRaises(SnapshotError):
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


if __name__ == '__main__':
    unittest.main()
