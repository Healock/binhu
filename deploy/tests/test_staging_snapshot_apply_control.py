import ast
import json
import sys
import subprocess
import unittest
from unittest.mock import patch
from deploy.environments.staging_data.apply_control import (
    CANDIDATE_CREATE_TIMEOUT_SECONDS, CANDIDATE_JOB_TIMEOUT_SECONDS,
    candidate_action_timeout, command, grant_sql, program, run_job,
)
from deploy.environments.staging_data.codec import SnapshotError


class ApplyControlTests(unittest.TestCase):
    def test_create_job_has_a_separate_bounded_timeout(self):
        self.assertEqual(candidate_action_timeout('create'), CANDIDATE_CREATE_TIMEOUT_SECONDS)
        self.assertEqual(candidate_action_timeout('create'), 900)
        for action in ('measure', 'import', 'verify'):
            with self.subTest(action=action):
                self.assertEqual(candidate_action_timeout(action), CANDIDATE_JOB_TIMEOUT_SECONDS)
                self.assertEqual(candidate_action_timeout(action), 300)

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
            run_job({'Image':'sha256:'+'a'*64},code,'binhu-staging-snapshot-test',timeout=900)
        args=call.call_args.args[0]
        self.assertEqual(args[args.index('--network')+1],'binhu-staging_internal')
        self.assertIn('--memory',args)
        self.assertNotIn('--volume',args)
        self.assertNotIn('-v',args)
        self.assertIn('--read-only',args)
        self.assertEqual(call.call_args.kwargs['timeout'],900)

    def test_embedded_candidate_program_includes_runtime_table_filter(self):
        code,_=program('staging-'+'a'*16,'measure')
        self.assertIn('def runtime_tables(tables, domain):',code)
        for action in ('measure','verify'):
            generated,_=program('staging-'+'a'*16,action)
            names={node.id for node in ast.walk(ast.parse(generated))
                   if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load)}
            self.assertNotIn('SPLIT_DOMAIN_SCHEMA_TABLES',names)
        namespace={}
        # Execute only the self-contained loader prefix; imports requiring the
        # server runtime are intentionally outside this unit test boundary.
        prefix=code.split('from config import settings',1)[0]
        import io
        with patch('sys.stdin',io.StringIO('{}')):
            exec(prefix,namespace)
        self.assertEqual(
            namespace['runtime_tables'](
                {'_communities','_police_address_entries','_continuation_import_runs'},
                'PlatformData'),
            {'_communities'},
        )

    def test_measure_and_verify_require_source_observation_and_target_tables(self):
        measure,_=program('staging-'+'a'*16,'measure')
        verify,_=program('staging-'+'a'*16,'verify')
        for code in (measure,verify):
            self.assertIn("source_schema_contract_missing",code)
            self.assertIn("snapshot_tables_missing",code)
            self.assertIn("snapshot_table_identifier_invalid",code)
            self.assertIn("information_schema.statistics",code)
            self.assertIn("information_schema.table_constraints",code)
        self.assertIn("ready_for_application_switch':True",verify)

    def test_target_contract_diagnostics_include_required_table_diff(self):
        code,_=program('staging-'+'a'*16,'measure')
        self.assertIn("'missing_tables':sorted(missing)",code)
        self.assertIn("'expected_table_count':len(required_by_domain[domain])",code)
        self.assertIn("'actual_table_count':len(actual_tables)",code)
        self.assertIn("'_domain_migration_state'",code)
        self.assertIn("'_police_dispatch_publish_run_items'",code)
        self.assertIn("runtime_tables(tables, domain)",code)

    def test_job_failure_preserves_safe_schema_diagnostics(self):
        code,_=program('staging-'+'a'*16,'measure')
        output='{"ok":false,"reason":"production_staging_schema_table_mismatch","diagnostics":{"domain":"OnlineData","missing_tables":["new_table"],"extra_tables":["old_table"],"expected_table_count":4,"actual_table_count":4}}'
        with patch('deploy.environments.staging_data.apply_control.command',return_value=output):
            with self.assertRaises(SnapshotError) as caught:
                run_job({'Image':'sha256:'+'a'*64},code,'binhu-staging-snapshot-test')
        self.assertEqual(caught.exception.diagnostics['missing_tables'],['new_table'])
        self.assertEqual(caught.exception.diagnostics['extra_tables'],['old_table'])

    def test_schema_signature_diagnostics_include_bounded_structural_diff(self):
        code,_=program('staging-'+'a'*16,'measure')
        self.assertIn("'missing_columns':sorted",code)
        self.assertIn("'extra_indexes':sorted",code)
        self.assertIn("'missing_constraints':sorted",code)
        self.assertIn("'expected_signature_sha256':digest(expected)",code)

    def test_embedded_signature_diff_runs_with_its_declared_imports(self):
        code,_=program('staging-'+'a'*16,'measure')
        parsed=ast.parse(code)
        signature_diff=next(node for node in parsed.body
                            if isinstance(node,ast.FunctionDef) and node.name=='signature_diff')
        imports=[node for node in parsed.body if isinstance(node,ast.Import) and node.lineno==1]
        namespace={}
        exec(compile(ast.Module(body=imports+[signature_diff],type_ignores=[]),
                     '<embedded-signature-diff>','exec'),namespace)
        expected={'columns':[['old_col']],'indexes':[],'constraints':[]}
        actual={'columns':[['new_col']],'indexes':[],'constraints':[]}
        diff=namespace['signature_diff'](expected,actual)
        self.assertEqual(diff['missing_columns'],['old_col'])
        self.assertEqual(diff['extra_columns'],['new_col'])
        self.assertRegex(diff['expected_signature_sha256'],r'^[0-9a-f]{64}$')
        self.assertRegex(diff['actual_signature_sha256'],r'^[0-9a-f]{64}$')

    def test_job_failure_preserves_signature_diff_without_values(self):
        code,_=program('staging-'+'a'*16,'measure')
        output=('{{"ok":false,"reason":"production_staging_schema_mismatch",'
                '"diagnostics":{{"domain":"OnlineData","table_name":"_online_source_projection",'
                '"schema_signature_mismatch":true,"expected_signature_sha256":"{}",'
                '"actual_signature_sha256":"{}","missing_columns":["new_col"],'
                '"extra_indexes":["old_idx"]}}}}').format('a'*64,'b'*64)
        with patch('deploy.environments.staging_data.apply_control.command',return_value=output):
            with self.assertRaises(SnapshotError) as caught:
                run_job({'Image':'sha256:'+'a'*64},code,'binhu-staging-snapshot-test')
        self.assertEqual(caught.exception.diagnostics['missing_columns'],['new_col'])
        self.assertEqual(caught.exception.diagnostics['extra_indexes'],['old_idx'])

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
