import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import development_application_gateway as gateway

ROOT = Path(__file__).parents[2]

class DevApplicationGatewayTests(unittest.TestCase):
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

    def test_workflow_has_fixed_four_actions_and_dev_secrets(self):
        workflow = (ROOT / '.github/workflows/promote-dev-application.yml').read_text()
        self.assertIn('options: [prepare, measure, apply, accept]', workflow)
        self.assertIn('BINHU_DEV_APP_SSH_KEY', workflow)
        self.assertIn('binhu-dev-app-deploy@', workflow)
        self.assertNotIn('BINHU_STAGING', workflow)
        install = (ROOT / '.github/workflows/install-dev-application-gateway.yml').read_text()
        self.assertIn('BINHU_DEV_APP_ADMIN_SSH_KEY', install)
        self.assertIn('BINHU_DEV_APP_DEPLOY_PUBLIC_KEY', install)
        self.assertIn('binhu-dev-app-deploy', install)

    def test_install_script_has_no_production_or_staging_target(self):
        script = (ROOT / 'deploy/environments/install-dev-application-gateway.sh').read_text()
        self.assertIn('development_application_gateway.py', script)
        for module in ('artifact.py', 'image.py', 'runtime.py', 'update.py'):
            self.assertIn(f'deploy/environments/{module}', script)
        self.assertIn('/usr/local/libexec/binhu-dev-application/deploy/environments', script)
        self.assertIn('binhu-dev-app-deploy', script)
        self.assertNotIn('production', script.lower())
        self.assertNotIn('staging', script.lower())

if __name__ == '__main__':
    unittest.main()
