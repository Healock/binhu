import json
import os
import tempfile
import unittest
from pathlib import Path
from deploy.environments.development_eventbus import prepare
from deploy.environments.development_shadow_migrate import extract


class DevEventbusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.src = self.root / 'shadow'
        self.src.mkdir()
        self.compose = self.src / 'docker-compose.yml'
        self.compose.write_text('services:\n  db:\n    environment:\n      MYSQL_PASSWORD: synthetic-secret\n    volumes:\n      - /srv/production:/data\n', encoding='utf-8')

    def test_evidence_never_contains_yaml_secrets_or_old_mounts(self):
        (self.src / '.env').write_text('TOKEN=synthetic-token')
        artifacts = self.src / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'result.yml').write_text('private: synthetic-pii')
        output = self.root / 'evidence'
        manifest = prepare(self.src, output, 'dev-test-1')
        self.assertEqual([p.name for p in output.iterdir()], ['manifest.json'])
        text = (output / 'manifest.json').read_text()
        for secret in ('synthetic-secret', 'synthetic-token', 'synthetic-pii', '/srv/production'):
            self.assertNotIn(secret, text)
        self.assertFalse(manifest['state_copied'])
        self.assertFalse(manifest['runtime_generated'])
        self.assertEqual(manifest['acceptance'], 'pending')
        self.assertIn('docker-compose.yml', manifest['files'])
        self.assertEqual(manifest['files']['docker-compose.yml']['bytes'], self.compose.stat().st_size)

    def test_nested_output_rejected_without_creating_files(self):
        output = self.src / 'evidence'
        with self.assertRaises(ValueError):
            extract(self.src, output)
        self.assertFalse(output.exists())

    def test_existing_evidence_not_overwritten(self):
        output = self.root / 'evidence'
        extract(self.src, output)
        before = (output / 'manifest.json').read_bytes()
        self.compose.write_text('changed')
        with self.assertRaises(ValueError):
            extract(self.src, output)
        self.assertEqual(before, (output / 'manifest.json').read_bytes())

    def test_relative_paths_and_production_run_rejected(self):
        with self.assertRaises(ValueError):
            extract(Path('shadow'), self.root / 'out')
        with self.assertRaises(ValueError):
            prepare(self.src, self.root / 'out', 'production-1')
        self.assertFalse((self.root / 'out').exists())

    def test_symlink_input_rejected_before_resolution(self):
        link = self.root / 'alias'
        try:
            link.symlink_to(self.src, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable on this host')
        with self.assertRaises(ValueError):
            extract(link, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_symlink_config_rejected(self):
        link = self.src / 'docker-compose.business.yml'
        try:
            link.symlink_to(self.compose)
        except OSError:
            self.skipTest('symlink creation unavailable on this host')
        with self.assertRaises(ValueError):
            extract(self.src, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission check')
    def test_private_permissions(self):
        output = self.root / 'evidence'
        extract(self.src, output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((output / 'manifest.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
