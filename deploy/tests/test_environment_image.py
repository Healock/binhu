import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.tests.test_environment_artifact import EnvironmentArtifactTests
from deploy.environments.artifact import build_artifact
from deploy.environments.image import build_image, verify_image


class EnvironmentImageTests(unittest.TestCase):
    def candidate(self, root):
        fixture = EnvironmentArtifactTests()
        repo, commit = fixture.source_repo(root)
        with patch('deploy.environments.artifact.run_frontend_build', side_effect=fixture.fake_build):
            manifest = build_artifact(repo, commit, root / 'artifact')
        return manifest

    def test_build_uses_archived_source_and_binds_image_to_expected_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.candidate(root)
            (root / 'artifact/source/backend/Dockerfile').write_text('DIRTY')
            image = 'sha256:' + 'a' * 64
            def fake_build(context, output, labels):
                self.assertNotIn('DIRTY', (context / 'Dockerfile').read_text())
                self.assertEqual((context.parent / 'VERSION').read_text().strip(), manifest['version'])
                self.assertEqual(labels['org.opencontainers.image.revision'], manifest['commit'])
                self.assertEqual(labels['binhu.artifact.id'], manifest['artifact_id'])
                return image
            with patch('deploy.environments.image.docker_build', side_effect=fake_build), \
                    patch('deploy.environments.image.inspect_image') as inspect:
                inspect.return_value = {'Id': image, 'Config': {'Labels': {
                    'org.opencontainers.image.revision': manifest['commit'],
                    'org.opencontainers.image.version': manifest['version'],
                    'binhu.artifact.id': manifest['artifact_id'],
                }, 'Env': ['APP_VERSION=' + manifest['version']]}}
                result = build_image(root / 'artifact', manifest['artifact_id'], root / 'image')
                self.assertEqual(result['image_id'], image)
                self.assertEqual(verify_image(root / 'artifact', manifest['artifact_id'], root / 'image'), result)

    def test_wrong_expected_artifact_cannot_start_docker_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.candidate(root)
            with patch('deploy.environments.image.docker_build') as build:
                with self.assertRaisesRegex(ValueError, 'artifact_identity_mismatch'):
                    build_image(root / 'artifact', 'f' * 64, root / 'image')
                build.assert_not_called()
                self.assertFalse((root / 'image').exists())

    def test_docker_failure_preserves_failure_marker_and_cannot_overwrite_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.candidate(root)
            with patch('deploy.environments.image.docker_build', side_effect=RuntimeError('private details')):
                with self.assertRaisesRegex(RuntimeError, 'environment_image_build_failed'):
                    build_image(root / 'artifact', manifest['artifact_id'], root / 'image')
            self.assertEqual(json.loads((root / 'image/failure.json').read_text()),
                             {'error': 'environment_image_build_failed'})
            with self.assertRaisesRegex(ValueError, 'image_output_exists'):
                build_image(root / 'artifact', manifest['artifact_id'], root / 'image')


if __name__ == '__main__':
    unittest.main()
