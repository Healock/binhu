import ast
import json
import sys
import subprocess
import unittest
from unittest.mock import patch
from deploy.environments.staging_data.apply_control import command,grant_sql,program,run_job
from deploy.environments.staging_data.codec import SnapshotError


class ApplyControlTests(unittest.TestCase):
    def test_command_failure_keeps_exit_code_without_driver_output(self):
        result=subprocess.CompletedProcess(['synthetic'],137,'private-output','private-error')
        with patch('subprocess.run',return_value=result):
            with self.assertRaises(SnapshotError) as caught:command(['synthetic'])
        self.assertEqual(str(caught.exception),'staging_candidate_command_failed')
        self.assertEqual(caught.exception.diagnostics,{'exit_code':137})

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

    def test_import_data_is_json_on_stdin_not_python_source_or_arguments(self):
        data={'tables':{'fixture_only_payload_marker':[{'text':'虚构\\n\"sample', 'active':True, 'value':None}]*10000}}
        code,_=program('staging-'+'a'*16,'import')
        # A large input must never expand the Python compiler AST or argv.
        self.assertLess(len(list(ast.walk(ast.parse(code)))), 2000)
        with patch('deploy.environments.staging_data.apply_control.command',return_value='{"ok":true,"result":{}}') as call:
            run_job({'Image':'sha256:'+'a'*64},code,'binhu-staging-snapshot-test',snapshot=data)
        args=call.call_args.args[0]
        self.assertEqual(args[-2:],['-c',code])
        self.assertEqual(json.loads(call.call_args.kwargs['stdin']),data)
        self.assertNotIn('fixture_only_payload_marker',code)
        # Run the actual generated loader, stopping before app imports.
        import io
        namespace={}
        with patch.dict(sys.modules), patch('sys.stdin',io.StringIO(json.dumps(data))):
            exec(code.split('from config import settings')[0],namespace)
        self.assertEqual(namespace['snapshot'],data)


if __name__=='__main__':unittest.main()
