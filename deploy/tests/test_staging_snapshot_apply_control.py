import unittest
from unittest.mock import patch
from deploy.environments.staging_data.apply_control import grant_sql,program,run_job
from deploy.environments.staging_data.codec import SnapshotError


class ApplyControlTests(unittest.TestCase):
    def test_grants_only_match_eight_exact_candidate_databases(self):
        sql=grant_sql('staging-'+'a'*16)
        self.assertEqual(len(sql.splitlines()),8)
        self.assertNotIn('IDENTIFIED BY',sql)
        self.assertNotIn('ON *.*',sql)
        self.assertIn(r'Staging\_saaaaaaaaaaaaaaaa\_OnlineData',sql)
        with self.assertRaises(SnapshotError):grant_sql('production')

    def test_candidate_program_compiles_and_job_has_no_shared_volumes(self):
        code,_=program('staging-'+'a'*16,'measure')
        compile(code,'<candidate-job>','exec')
        with patch('deploy.environments.staging_data.apply_control.command',return_value='{"ok":true,"result":{}}') as call:
            run_job({'Image':'sha256:'+'a'*64},code,'binhu-staging-snapshot-test')
        args=call.call_args.args[0]
        self.assertEqual(args[args.index('--network')+1],'binhu-staging_internal')
        self.assertIn('--memory',args)
        self.assertNotIn('--volume',args)
        self.assertNotIn('-v',args)
        self.assertIn('--read-only',args)


if __name__=='__main__':unittest.main()
