import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import staging_application_gateway as application
from deploy.environments import staging_data_gateway as data
from deploy.environments.staging_data.codec import SnapshotError


ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = ROOT / 'deploy/environments'


class StagingApplicationGatewayTests(unittest.TestCase):
    def test_artifact_archive_has_exact_fixed_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode='w:gz') as archive:
                for name in application.ARCHIVE_MEMBERS:
                    source = root / name
                    source.write_bytes(b'x')
                    archive.add(source, arcname=name)
            target = root / 'out'
            target.mkdir()
            application._extract_artifact(payload.getvalue(), target)
            self.assertEqual({path.name for path in target.iterdir()}, application.ARCHIVE_MEMBERS)

            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode='w:gz') as archive:
                source = root / 'extra'
                source.write_bytes(b'x')
                archive.add(source, arcname='arbitrary-command')
            with self.assertRaisesRegex(ValueError, 'contents invalid'):
                application._extract_artifact(payload.getvalue(), target)

    def test_only_fixed_staging_run_and_immutable_identities_are_accepted(self):
        for value in ('production', 'development', 'shadow', '../../staging-app-' + 'a' * 16):
            with self.subTest(value=value), self.assertRaises(ValueError):
                application._run_root(value)
        self.assertEqual(application._run_root('staging-app-' + 'a' * 16).name,
                         'staging-app-' + 'a' * 16)

    def test_accept_requires_matching_live_dev_and_evidence(self):
        manifest = {'artifact_id': 'a' * 64, 'image_id': 'sha256:' + 'b' * 64,
                    'commit': 'c' * 40, 'version': '1.2.3'}
        report = {'environment': 'development', 'health': True, **manifest}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(application, 'EVIDENCE_ROOT', Path(directory)), \
                patch.object(application, 'root_for', return_value=Path(directory)), \
                patch.object(application, 'read_configuration', return_value=(
                    {'artifact_id': manifest['artifact_id'], 'images': {'backend': manifest['image_id']},
                     'commit': manifest['commit'], 'version': manifest['version']}, {}, '')):
            run = 'dev-update-' + 'd' * 16
            path = Path(directory) / run
            path.mkdir()
            (path / 'result.json').write_text(json.dumps(report))
            self.assertTrue(application._dev_acceptance(run, manifest)['health'])
            report['image_id'] = 'sha256:' + 'e' * 64
            (path / 'result.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'same immutable candidate'):
                application._dev_acceptance(run, manifest)

    def test_wrapper_and_sudo_contract_have_no_environment_selector(self):
        wrapper = (ENVIRONMENTS / 'binhu-staging-application-gateway').read_text(encoding='utf-8')
        installer = (ENVIRONMENTS / 'install-staging-promotion-gateways.sh').read_text(encoding='utf-8')
        for action in ('prepare)', 'measure|apply)', 'accept)'):
            self.assertIn(action, wrapper)
        self.assertNotIn('--environment', wrapper)
        self.assertNotIn('production-deploy', installer)
        self.assertIn('binhu-staging-app-deploy', installer)
        self.assertIn('/usr/local/libexec/binhu-staging-application-gateway apply *', installer)
        self.assertIn('staging_gateway_authorization_boundary_installed', installer)
        self.assertIn('control-commit', installer)
        self.assertIn('mktemp -d "$deploy_root/.environments.new.', installer)
        self.assertIn('python3 -m compileall -q "$staged_environments"', installer)
        self.assertIn('mv "$staged_environments" "$deploy_root/environments"', installer)
        self.assertIn(
            'install -d -o root -g root -m 0700 /var/lib/binhu-staging-application\n',
            installer,
        )
        source = (ENVIRONMENTS / 'staging_application_gateway.py').read_text(encoding='utf-8')
        self.assertNotIn('build_image(', source)
        self.assertIn('_adopt_dev_image(', source)
        self.assertIn('_dev_acceptance(manifest["dev_acceptance_run_id"], manifest)', source)


class StagingDataGatewayTests(unittest.TestCase):
    def test_gateway_exposes_only_fixed_snapshot_contract(self):
        wrapper = (ENVIRONMENTS / 'binhu-staging-data-gateway').read_text(encoding='utf-8')
        installer = (ENVIRONMENTS / 'install-staging-promotion-gateways.sh').read_text(encoding='utf-8')
        self.assertIn('approved-sanitized-scope-v1', wrapper)
        self.assertIn('create|import|verify|switch', wrapper)
        self.assertNotIn('--path', wrapper)
        self.assertNotIn('--database', wrapper)
        self.assertIn('binhu-staging-data-deploy', installer)
        self.assertNotIn('ON *.*', installer)

    def test_export_enables_only_two_reviewed_recovery_policies(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(data, 'ROOT', Path(directory)), \
                patch.object(data, '_snapshot', return_value=Path(directory)), \
                patch.object(data, '_record'), patch.object(data, '_audit'), \
                patch.object(data, 'source_execute', return_value={'snapshot_id': 'staging-' + 'a' * 16}) as execute:
            (Path(directory) / 'report.json').write_text(json.dumps({
                'sensitive_value_matches': 0, 'reference_integrity': True}))
            data.export(data.POLICY)
            execute.assert_called_once_with('export', exclude_orphan_property_links=True,
                                            recover_model_three_sources=True)
            with self.assertRaises(SnapshotError):
                data.export('arbitrary')

    def test_import_replay_returns_existing_record_without_second_write(self):
        snapshot = 'staging-' + 'a' * 16
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'gateway-export.json').write_text('{}')
            (root / 'gateway-create.json').write_text('{}')
            expected = {'snapshot_id': snapshot, 'idempotent_replay': True}
            (root / 'gateway-import.json').write_text(json.dumps(expected))
            with patch.object(data, '_snapshot', return_value=root), \
                    patch.object(data, 'candidate_execute') as execute:
                self.assertEqual(data.candidate('import', snapshot), expected)
                execute.assert_not_called()

    def test_switch_refuses_before_verification(self):
        snapshot = 'staging-' + 'a' * 16
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(data, '_snapshot', return_value=Path(directory)), \
                patch.object(data, 'switch_snapshot') as switch:
            with self.assertRaises(SnapshotError):
                data.switch(snapshot)
            switch.assert_not_called()

    def test_workflows_use_separate_staging_keys_and_fixed_users(self):
        install = (ROOT / '.github/workflows/install-staging-promotion-gateways.yml').read_text(encoding='utf-8')
        application_flow = (ROOT / '.github/workflows/promote-staging-application.yml').read_text(encoding='utf-8')
        data_flow = (ROOT / '.github/workflows/manage-staging-sanitized-snapshot.yml').read_text(encoding='utf-8')
        self.assertIn('BINHU_STAGING_APP_SSH_KEY', install + application_flow)
        self.assertIn('BINHU_STAGING_DATA_SSH_KEY', install + data_flow)
        self.assertIn('bash ./deploy/environments/install-staging-promotion-gateways.sh', install)
        self.assertEqual(install.count('BINHU_STAGING_PORT'), 2)
        self.assertEqual(application_flow.count('BINHU_STAGING_PORT'), 1)
        self.assertEqual(data_flow.count('BINHU_STAGING_PORT'), 1)
        self.assertIn('ssh -p "$PORT"', install)
        self.assertIn('ssh_command=(ssh -p "$PORT"', application_flow)
        self.assertIn('ssh_command=(ssh -p "$PORT"', data_flow)
        self.assertIn('binhu-staging-app-deploy@', application_flow)
        self.assertIn('binhu-staging-data-deploy@', data_flow)
        self.assertNotIn('binhu-deploy@', install + application_flow + data_flow)
        self.assertIn('tar -C "$package"', install)
        self.assertNotIn('/tmp/staging-app.pub', install)


if __name__ == '__main__':
    unittest.main()
