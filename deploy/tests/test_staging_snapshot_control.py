import tempfile
import unittest
from pathlib import Path
from deploy.environments.staging_data.control import private_json, source_program, safe_diagnostics


class ControlTests(unittest.TestCase):
    def test_reader_program_compiles_and_never_logs_connection_secrets(self):
        program, hashes = source_program('staging-'+'a'*16, b'a'*32, measure=True)
        compile(program, '<snapshot-reader>', 'exec')
        self.assertEqual(set(hashes), {'codec','registry','tasks','fences','organization','relations','digests','build'})
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
        self.assertEqual(value['secret'], 'redacted')
        self.assertNotIn('张三', repr(value))


if __name__ == '__main__':
    unittest.main()
