import tempfile
import subprocess
import sys
import unittest
import json
from unittest.mock import patch, Mock
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from deploy.environments.staging_data.control import private_json, source_program, safe_diagnostics


class ControlTests(unittest.TestCase):
    def test_package_read_failure_is_recorded_without_exception_text(self):
        from deploy.environments.staging_data import control
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
            with patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                    patch.object(control, 'ROOT', root), \
                    patch.object(control.os, 'umask'), \
                    patch.object(control, 'safe_directory'), \
                    patch.object(control, 'preflight', return_value={'container_id':'synthetic'}), \
                    patch.object(control, 'source_program', side_effect=FileNotFoundError('private-path')), \
                    patch.object(control.subprocess, 'run') as run:
                with self.assertRaises(control.SnapshotError):
                    control.execute('measure')
                failures = list(root.glob('staging-*/failure.json'))
                self.assertEqual(len(failures), 1)
                self.assertNotIn('private-path', failures[0].read_text())
                self.assertEqual(json.loads(failures[0].read_text())['reason'], 'snapshot_operation_failed')
                run.assert_not_called()

    def test_diagnostic_parser_contract_matches_current_business_fields(self):
        from deploy.environments.staging_data.diagnostic_contract import TASK_COLUMNS
        from deploy.environments.staging_data.tasks import TASK_TYPES
        from services.parsers import get_parser
        expected = {'OnlineData.' + get_parser(t).table_name: tuple(get_parser(t).COLUMNS)
                    for t in TASK_TYPES}
        self.assertEqual(TASK_COLUMNS, expected)

    def test_host_diagnostics_require_no_backend_imports(self):
        root = str(Path(__file__).resolve().parents[2])
        code = ('import sys; sys.path.insert(0, ' + repr(root) + '); '
                'from deploy.environments.staging_data.control import safe_diagnostics; '
                'assert safe_diagnostics({"source_count": 1}) == {"source_count": 1}')
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_unknown_table_or_column_is_not_a_safe_identifier(self):
        rows = [{'table':'OnlineData.t_fullchain','field':'SyntheticSecret-abc','count':1},
                {'table':'SyntheticSecret_abc','field':'status','count':1},
                {'table':'RegistryData.registry_properties','field':'status','count':2}]
        self.assertEqual(safe_diagnostics({'fields':rows}), {'fields':[rows[2]]})

    def test_reader_program_compiles_and_never_logs_connection_secrets(self):
        program, hashes = source_program('staging-'+'a'*16, b'a'*32, measure=True)
        compile(program, '<snapshot-reader>', 'exec')
        self.assertEqual(set(hashes), {'codec','registry','tasks','fences','organization','relations','digests','reconciliation','build','recovery'})
        self.assertIn("source_settings(settings)", program)
        self.assertIn("'snapshot_source_operation_failed'", program)
        self.assertNotIn('print(settings', program)
        self.assertNotIn('print(str(exc))', program)
        self.assertNotIn('traceback.print', program)

    def test_existing_failure_evidence_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'failure.json'
            private_json(path, {'reason':'first_failure'})
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                private_json(path, {'reason':'second_attempt'})
            self.assertEqual(path.read_bytes(), original)

    def test_diagnostics_are_aggregate_and_do_not_echo_free_text(self):
        value = safe_diagnostics({
            'parser_type': '全链条',
            'source_count': 2,
            'secret': '姓名张三13800138000',
            'nested': {'field': 'ok'},
        })
        self.assertEqual(value['source_count'], 2)
        self.assertNotIn('secret', value)
        self.assertNotIn('张三', repr(value))

    def test_ascii_credentials_and_arbitrary_nested_keys_are_not_safe_metadata(self):
        self.assertEqual(safe_diagnostics({'password': 'SyntheticToken-ABC123',
            'nested': {'field': 'Bearer SyntheticToken'}, 'source_count': '19900000000'}), {})
        deep = {'source_count': 1}
        for _ in range(100):
            deep = {'nested': deep}
        self.assertEqual(safe_diagnostics(deep), {})


if __name__ == '__main__':
    unittest.main()
