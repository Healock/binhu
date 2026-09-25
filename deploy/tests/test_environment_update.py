import contextlib
import copy
import io
import itertools
import json
from pathlib import Path
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy.environments import runtime
from deploy.environments import update
from deploy.environments.artifact import tree_hashes
from deploy.environments.update import (candidate_configuration, canonical_database_manifest,
                                         image_digest_reference, parse_environment, read_configuration)
from deploy.tests import test_environment_static_preparation as fixtures


class EnvironmentUpdateTests(unittest.TestCase):
    def test_image_digest_reference_accepts_qualified_compose_reference(self):
        digest = 'sha256:' + 'a' * 64
        self.assertEqual(update.image_digest_reference('binhu-backend@' + digest), digest)
        self.assertEqual(image_digest_reference(digest), digest)

    def test_image_digest_reference_rejects_mutable_or_malformed_reference(self):
        with self.assertRaisesRegex(ValueError, 'environment_image_reference_invalid'):
            image_digest_reference('binhu-backend:latest')

    def test_canonical_database_manifest_maps_legacy_runtime_keys(self):
        values = {f'MYSQL_{key}_DB': 'Staging_' + domain
                  for key, domain in zip(update.KEYS, update.DOMAINS)}
        self.assertEqual(canonical_database_manifest(values),
                         dict(zip(update.DOMAINS, ('Staging_' + domain for domain in update.DOMAINS))))

    def fixture(self, root, environment='development'):
        args = fixtures.EnvironmentStaticPreparationTests().prepare(
            root, '<html><head><script type="module" src="./assets/app.js"></script></head></html>')
        args.environment = environment
        target = root / environment
        with patch.object(runtime, 'root_for', return_value=target), \
                patch.object(runtime, 'image_id', side_effect=lambda value: value), \
                patch.object(runtime, 'command', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            runtime.prepare(args)
        return target, read_configuration(target)

    def image(self):
        return {'image_id': 'sha256:' + 'a' * 64, 'artifact_id': 'b' * 64,
                'commit': 'c' * 40, 'version': '0.28.15'}

    def test_candidate_preserves_databases_credentials_and_nonbackend_services(self):
        with tempfile.TemporaryDirectory() as directory:
            target, (manifest, compose, env_text) = self.fixture(Path(directory))
            original = copy.deepcopy(compose)
            candidate, new_env, new_manifest = candidate_configuration(
                'development', target, manifest, compose, env_text, self.image())
            self.assertEqual(compose, original)
            for name in ('environment-mysql', 'redis'):
                self.assertEqual(candidate['services'][name], compose['services'][name])
            for key in ('networks', 'volumes', 'name'):
                self.assertEqual(candidate[key], compose[key])
            old_values = parse_environment(env_text)
            old_values['APP_VERSION'] = self.image()['version']
            self.assertEqual(parse_environment(new_env), old_values)
            self.assertEqual(new_manifest['databases'], manifest['databases'])
            self.assertEqual(candidate['services']['backend']['image'], self.image()['image_id'])
            self.assertIn(self.image()['artifact_id'], candidate['services']['backend']['volumes'][0])

    def test_staging_candidate_uses_only_staging_identity_and_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            target, (manifest, compose, env_text) = self.fixture(Path(directory), 'staging')
            candidate, new_env, new_manifest = candidate_configuration(
                'staging', target, manifest, compose, env_text, self.image())
            values = parse_environment(new_env)
            self.assertEqual(values['APP_ENVIRONMENT'], 'staging')
            self.assertEqual(values['SESSION_COOKIE_NAME'], 'binhu_staging_session')
            self.assertTrue(all(name.startswith('Staging_') for name in new_manifest['databases'].values()))
            self.assertEqual(candidate['name'], 'binhu-staging')
            self.assertEqual(candidate['networks']['internal']['name'], 'binhu-staging_internal')
            self.assertEqual(candidate['volumes']['mysql']['name'], 'binhu-staging_mysql')
            self.assertEqual(candidate['services']['backend']['ports'], ['127.0.0.1:48126:37125'])
            self.assertNotIn('Dev_', new_env)

    def test_staging_rejects_development_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            target, (manifest, compose, env_text) = self.fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, 'environment_identity_mismatch'):
                candidate_configuration(
                    'staging', target, manifest, compose, env_text, self.image())

    def test_production_target_foreign_network_overrides_and_mounts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target, (manifest, compose, env_text) = self.fixture(Path(directory))
            for kind in ('production', 'network', 'volume', 'env_override', 'port', 'database', 'external', 'external_monitor'):
                modified = copy.deepcopy(compose)
                altered_env = env_text
                environment = 'development'
                if kind == 'production': environment = 'production'
                if kind == 'network': modified['networks']['internal']['name'] = 'binhu_default'
                if kind == 'volume': modified['services']['backend']['volumes'] = ['/srv/binhu:/app:ro']
                if kind == 'env_override': modified['services']['backend']['environment'] = {'MYSQL_HOST': 'production'}
                if kind == 'port': modified['services']['backend']['ports'] = ['48125:37125']
                if kind == 'database': altered_env = env_text.replace('MYSQL_ONLINE_DATA_DB=Dev_OnlineData', 'MYSQL_ONLINE_DATA_DB=OnlineData')
                if kind == 'external': altered_env = env_text.replace('TXDOCS_ENABLED=false', 'TXDOCS_ENABLED=true')
                if kind == 'external_monitor': altered_env = env_text.replace('TXDOCS_MONITORING_ENABLED=false', 'TXDOCS_MONITORING_ENABLED=true')
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    candidate_configuration(environment, target, manifest, modified, altered_env, self.image())

    def test_configuration_changes_after_measurement_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self.fixture(Path(directory))
            with (target / 'backend.env').open('a') as stream:
                stream.write('APP_VERSION=other\n')
            with self.assertRaisesRegex(ValueError, 'environment_configuration_drift'):
                read_configuration(target)

    def test_duplicate_environment_keys_do_not_shadow_verified_values(self):
        with self.assertRaisesRegex(ValueError, 'environment_configuration_invalid'):
            parse_environment('APP_ENVIRONMENT=development\nAPP_ENVIRONMENT=production\n')

    def test_application_update_and_failures_keep_database_and_rollback_boundaries(self):
        for environment, failure in itertools.product(
                ('development', 'staging'), ('none', 'backup', 'health')):
            with self.subTest(environment=environment, failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                target, original = self.fixture(root, environment)
                artifact = root / 'artifact'
                artifact.mkdir()
                with tarfile.open(artifact / 'frontend.tar', 'w') as archive:
                    archive.add(target / 'static/index.html', arcname='index.html')
                evidence_prefix = 'dev' if environment == 'development' else environment
                evidence = root / (evidence_prefix + '-update-' + 'a' * 16)
                image = self.image()
                calls = []
                def command(arguments):
                    calls.append(arguments)
                    if arguments[:2] == ['docker', 'inspect']:
                        return json.dumps([{'Image': image['image_id'], 'Config': {
                            'Labels': {'com.docker.compose.project': 'binhu-' + environment},
                            'Env': ['APP_VERSION=' + image['version']]}}])
                    return 'a' * 64
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.dict('sys.modules', {'fcntl': SimpleNamespace(
                        flock=Mock(), LOCK_EX=1, LOCK_NB=2)}))
                    stack.enter_context(patch.object(update, 'root_for', return_value=target))
                    stack.enter_context(patch.object(update, 'EVIDENCE_ROOT', root))
                    stack.enter_context(patch.object(update, 'verify_image', return_value=image))
                    stack.enter_context(patch.object(update, 'verify_artifact', return_value={
                        'frontend_files': tree_hashes(target / 'static')}))
                    identity = stack.enter_context(patch('deploy.environments.database_identity.run',
                        return_value={'all_markers_present': True}))
                    stack.enter_context(patch.object(update, 'check_resources'))
                    stack.enter_context(patch.object(update, 'container_baseline', return_value={}))
                    stack.enter_context(patch.object(update, 'command', side_effect=command))
                    backup = stack.enter_context(patch.object(update, 'backup_databases',
                        side_effect=RuntimeError('backup failed') if failure == 'backup' else None))
                    stack.enter_context(patch.object(update, 'health',
                        side_effect=ValueError('unhealthy') if failure == 'health' else None,
                        return_value={'health': True}))
                    stack.enter_context(patch.object(update.time, 'sleep'))
                    if failure == 'none':
                        result = update.apply_environment(
                            environment, artifact, root / 'image', image['artifact_id'], evidence)
                        self.assertFalse(result['business_acceptance'])
                        self.assertEqual(read_configuration(target)[0]['artifact_id'], image['artifact_id'])
                        self.assertEqual(identity.call_count, 2)
                    else:
                        with self.assertRaisesRegex(RuntimeError, environment + '_update_failed'):
                            update.apply_environment(
                                environment, artifact, root / 'image', image['artifact_id'], evidence)
                        self.assertEqual(read_configuration(target), original)
                        self.assertTrue((evidence / 'failure.json').exists())
                    backup.assert_called_once()
                updates = [arguments for arguments in calls if 'up' in arguments]
                self.assertEqual(len(updates), {'none': 1, 'backup': 0, 'health': 2}[failure])
                self.assertTrue(all(arguments[-4:] == ['up', '-d', '--no-deps', 'backend'] for arguments in updates))
