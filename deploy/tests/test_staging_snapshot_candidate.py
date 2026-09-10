import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from deploy.environments.staging_data.candidate import create
from deploy.environments.staging_data.codec import SnapshotError
from deploy.tests import test_staging_snapshot_target as target_tests


class Cursor:
    def __init__(self, existing=False):
        self.commands=[]
        self.existing=existing
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def execute(self,sql,params=()):
        self.commands.append(sql)
        if 'SELECT id,environment' in sql:self.result=[(1,'staging')]
        elif 'information_schema.tables' in sql:self.result=[('_environment_identity','InnoDB','BASE TABLE')]
        elif 'information_schema.schemata' in sql:self.result=[(int(self.existing),)]
        else:self.result=[(0,)]
    async def fetchall(self):return self.result
    async def fetchone(self):return self.result[0]


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_only_writes_new_databases(self):
        cur=Cursor()
        conn=SimpleNamespace(cursor=lambda:cur,commit=AsyncMock())
        await create(conn,target_tests.TargetTests().settings(),'staging-'+'a'*16)
        writes=[sql for sql in cur.commands if not sql.startswith('SELECT')]
        self.assertEqual(len(writes),24)
        for sql in writes:
            self.assertIn('`Staging_saaaaaaaaaaaaaaaa_',sql.split(' LIKE ')[0])
        self.assertFalse(any('DROP ' in sql or 'DELETE ' in sql for sql in cur.commands))

    async def test_existing_candidate_causes_zero_writes(self):
        cur=Cursor(existing=True)
        conn=SimpleNamespace(cursor=lambda:cur,commit=AsyncMock())
        with self.assertRaisesRegex(SnapshotError,'^candidate_database_already_exists$'):
            await create(conn,target_tests.TargetTests().settings(),'staging-'+'a'*16)
        self.assertTrue(all(sql.startswith('SELECT') for sql in cur.commands))


if __name__=='__main__':unittest.main()
