import copy
import json
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from types import SimpleNamespace
from unittest.mock import AsyncMock
from deploy.tests import test_staging_snapshot_target as target_tests
from deploy.environments.staging_data.import_data import import_rows, materialize
from deploy.environments.staging_data.codec import SnapshotError
from deploy.environments.staging_data.digests import identity_digest


def fixture():
    values={'身份证号':'990000190001010019','社区':'验证社区','核查结果':'待登记'}
    return {'report':{'snapshot_id':'staging-'+'a'*16,'sensitive_value_matches':0,'reference_integrity':True,
        'pending_gates':['registration_hmac_rebuild','candidate_database_import','target_verification']},
        'registration_digest_states':[{'parser_type':'全链条','row_key':'safe-key','identity':'current','address':'empty'}],
        'annotation_digest_states':[],
        'tables':{'OnlineData._online_source_rows':[{'parser_type':'全链条','row_key':'safe-key','values_json':json.dumps(values)}],
            'RegistryData.registry_properties':[],
            'PlatformData._users':[],
            'OnlineData._task_registration_links':[{'parser_type':'全链条','row_key':'safe-key','identity_hmac':'old-snapshot-digest','last_address_hmac':'','property_id':None}],
            'OnlineData._online_task_address_matches':[]}}


class Cursor:
    def __init__(self, fail=False):
        self.counts={}
        self.commands=[]
        self.fail=fail
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def execute(self,sql,params=()):
        self.commands.append(sql)
        if sql.startswith('SELECT id,environment'):
            self.result=[(1,'staging')]
        elif sql.startswith('SELECT COUNT(*) FROM '):
            self.result=[(self.counts.get(sql.split(' FROM ')[1],0),)]
    async def fetchall(self):return self.result
    async def fetchone(self):return self.result[0]
    async def executemany(self,sql,rows):
        self.commands.append(sql)
        if self.fail:raise RuntimeError('synthetic_driver_failure')
        table=sql.split('INSERT INTO ')[1].split(' (')[0]
        self.counts[table]=self.counts.get(table,0)+len(rows)


class ImportTests(unittest.IsolatedAsyncioTestCase):
    def test_digest_is_generated_using_staging_key_and_input_is_unchanged(self):
        original=fixture()
        before=copy.deepcopy(original)
        result=materialize(original,'synthetic-staging-key')
        self.assertEqual(result['OnlineData._task_registration_links'][0]['identity_hmac'],
            identity_digest(json.loads(original['tables']['OnlineData._online_source_rows'][0]['values_json']),
                            ['身份证号'],'synthetic-staging-key'))
        self.assertEqual(original,before)
        original['registration_digest_states'][0]['identity']='stale'
        result=materialize(original,'synthetic-staging-key')
        self.assertEqual(result['OnlineData._task_registration_links'][0]['identity_hmac'],'old-snapshot-digest')

    async def test_all_candidate_writes_commit_once_and_failure_rolls_back(self):
        settings=target_tests.TargetTests().settings()
        settings.registry_hmac_key='synthetic-staging-key'
        for fail in (False,True):
            cur=Cursor(fail)
            conn=SimpleNamespace(cursor=lambda:cur,begin=AsyncMock(),commit=AsyncMock(),rollback=AsyncMock())
            if fail:
                with self.assertRaisesRegex(RuntimeError,'synthetic_driver_failure'):
                    await import_rows(conn,settings,fixture())
                conn.rollback.assert_awaited_once()
                conn.commit.assert_not_awaited()
            else:
                report=await import_rows(conn,settings,fixture())
                conn.commit.assert_awaited_once()
                self.assertFalse(report['ready_for_application_switch'])
            self.assertTrue(all('`Staging_saaaaaaaaaaaaaaaa_' in sql for sql in cur.commands))

    def test_incomplete_preparation_cannot_be_imported(self):
        data=fixture()
        data['report']['pending_gates'].append('historical_event_summaries')
        with self.assertRaises(SnapshotError):materialize(data,'synthetic-key')


if __name__=='__main__':unittest.main()
