import json
import io
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import development_application_gateway as gateway

ROOT = Path(__file__).parents[2]

class DevApplicationGatewayTests(unittest.TestCase):
    def _transport(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, payload in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return output.getvalue()

    def test_extract_artifact_accepts_fixed_transport(self):
        data = self._transport({name: name.encode() for name in ('artifact.json', 'source.tar', 'frontend.tar')})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'artifact'
            target.mkdir()
            gateway._extract_artifact(data, target)
            self.assertEqual({'artifact.json', 'source.tar', 'frontend.tar'},
                             {path.name for path in target.iterdir()})

    def test_extract_artifact_rejects_empty_corrupt_and_missing_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'empty'
            target.mkdir()
            for data in (b'', b'not-a-tar', self._transport({'artifact.json': b'{}'})):
                with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                    gateway._extract_artifact(data, target)

    def test_fixed_run_and_environment_contract(self):
        self.assertTrue(gateway.RUN_RE.fullmatch('dev-update-' + 'a' * 16))
        for value in ('staging-app-' + 'a' * 16, 'dev-' + 'a' * 16, '../dev-update-' + 'a' * 16):
            self.assertIsNone(gateway.RUN_RE.fullmatch(value))
        source = (ROOT / 'deploy/environments/binhu-dev-application-gateway').read_text()
        self.assertIn('binhu-dev-application-gateway', source)
        self.assertNotIn('staging', source.lower())

    def test_manifest_rejects_non_development_or_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / 'candidates' / ('dev-update-' + 'a' * 16)
            candidate.mkdir(parents=True)
            payload = {'schema': 1, 'environment': 'staging', 'run_id': candidate.name,
                       'artifact_id': 'b' * 64, 'image_id': 'sha256:' + 'c' * 64,
                       'commit': 'd' * 40, 'version': '0.30.24', 'prepared_at': 1, 'state': 'prepared'}
            (candidate / 'promotion.json').write_text(json.dumps(payload))
            with patch.object(gateway, 'BASE', root), self.assertRaises(ValueError):
                gateway._read_manifest(candidate.name)

    def test_accept_never_reuses_event_pipeline_evidence(self):
        source = (ROOT / 'deploy/environments/development_application_gateway.py').read_text()
        self.assertNotIn('monitor40', source)
        self.assertIn('accepted', source)
        self.assertIn('health("development"', source)

    def test_prepare_failure_has_specific_error_code(self):
        self.assertEqual('gateway_missing_symbol', gateway._failure_code(NameError('x')))
        self.assertEqual('candidate_validation_failed', gateway._failure_code(ValueError('x')))
        self.assertEqual('candidate_runtime_failed', gateway._failure_code(RuntimeError('x')))

    def test_workflow_has_fixed_four_actions_and_dev_secrets(self):
        workflow = (ROOT / '.github/workflows/promote-dev-application.yml').read_text()
        self.assertIn('options: [prepare, measure, apply, accept]', workflow)
        self.assertIn('BINHU_DEV_APP_SSH_KEY', workflow)
        self.assertIn('binhu-dev-app-deploy@', workflow)
        self.assertIn('ARTIFACT_ID_BOUND', workflow)
        self.assertIn('or auto to bind the bundle', workflow)
        self.assertNotIn('BINHU_STAGING', workflow)
        install = (ROOT / '.github/workflows/install-dev-application-gateway.yml').read_text()
        self.assertIn('BINHU_DEV_APP_ADMIN_SSH_KEY', install)
        self.assertIn('BINHU_DEV_APP_DEPLOY_PUBLIC_KEY', install)
        self.assertIn('binhu-dev-app-deploy', install)

    def test_install_script_has_no_production_or_staging_target(self):
        script = (ROOT / 'deploy/environments/install-dev-application-gateway.sh').read_text()
        self.assertIn('development_application_gateway.py', script)
        for module in ('artifact.py', 'image.py', 'runtime.py', 'update.py'):
            self.assertIn(module, script)
        self.assertIn('/usr/local/libexec/binhu-dev-application/deploy/environments', script)
        self.assertIn('chmod 0700 /var/lib/binhu-dev-application', script)
        self.assertIn('binhu-dev-app-deploy', script)
        self.assertNotIn('production', script.lower())
        self.assertNotIn('staging', script.lower())

if __name__ == '__main__':
    unittest.main()
