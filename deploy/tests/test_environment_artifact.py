import json
import io
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from deploy.environments.artifact import build_artifact, verify_artifact, extract_source, json_hash, file_hash


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class EnvironmentArtifactTests(unittest.TestCase):
    def source_repo(self, root):
        repo = root / 'repository'
        repo.mkdir()
        git(repo, 'init')
        git(repo, 'config', 'user.name', 'Synthetic Builder')
        git(repo, 'config', 'user.email', 'builder@example.test')
        for name, value in {
            'VERSION': '0.28.15\n', '.gitignore': 'node_modules/\n',
            'backend/Dockerfile': 'FROM python:3.11-slim\n',
            'backend/init.sql': 'CREATE TABLE synthetic(id INT);\n',
            'frontend/package.json': '{}', 'frontend/package-lock.json': '{}',
            'frontend/index.html': 'committed source',
        }.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'synthetic source')
        return repo, git(repo, 'rev-parse', 'HEAD')

    def fake_build(self, source, evidence):
        self.assertEqual((source / 'frontend/index.html').read_text(), 'committed source')
        dist = source / 'frontend/dist-environment'
        (dist / 'assets').mkdir(parents=True)
        (dist / 'index.html').write_text('<html><head><script type="module" src="./assets/app.js"></script></head></html>')
        (dist / 'assets/app.js').write_text('export const synthetic = true;')

    def test_dirty_worktree_is_not_used_and_verified_package_binds_commit_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            (repo / 'frontend/index.html').write_text('uncommitted change')
            (repo / 'backend/.env').write_text('NOT_AN_ARTIFACT=synthetic')
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=self.fake_build):
                result = build_artifact(repo, commit, output)
            self.assertEqual(result['commit'], commit)
            self.assertEqual(result['version'], '0.28.15')
            self.assertFalse((output / 'source/backend/.env').exists())
            self.assertEqual(verify_artifact(output)['artifact_id'], result['artifact_id'])
            self.assertFalse(result['ready_for_staging'])

    def test_changed_archive_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=self.fake_build):
                build_artifact(repo, commit, output)
            with (output / 'frontend.tar').open('ab') as stream:
                stream.write(b'tampered')
            with self.assertRaisesRegex(ValueError, 'artifact_digest_mismatch'):
                verify_artifact(output)

    def test_failure_evidence_is_retained_and_existing_output_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=RuntimeError('private detail')):
                with self.assertRaisesRegex(RuntimeError, 'artifact_build_failed'):
                    build_artifact(repo, commit, output)
            self.assertEqual(json.loads((output / 'failure.json').read_text())['error'], 'artifact_build_failed')
            self.assertNotIn('private detail', (output / 'failure.json').read_text())
            with self.assertRaisesRegex(ValueError, 'artifact_output_exists'):
                build_artifact(repo, commit, output)

    def test_branch_names_are_not_accepted_as_immutable_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self.source_repo(root)
            with self.assertRaisesRegex(ValueError, 'artifact_commit_invalid'):
                build_artifact(repo, 'main', root / 'artifact')

    def test_manifest_cannot_claim_files_or_version_not_present_in_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=self.fake_build):
                original = build_artifact(repo, commit, output)
            for field, value in [('version', '9.9.9'), ('frontend_files', {'index.html': '0' * 64}),
                                 ('commit', 'f' * 40), ('backend_tree_sha256', '0' * 64),
                                 ('migration_sha256', '0' * 64)]:
                manifest = {**original, field: value}
                manifest['artifact_id'] = json_hash({k: v for k, v in manifest.items() if k != 'artifact_id'})
                (output / 'artifact.json').write_text(json.dumps(manifest))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'artifact_content_mismatch'):
                    verify_artifact(output)

    def test_tree_object_is_not_a_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _ = self.source_repo(root)
            tree = git(repo, 'rev-parse', 'HEAD^{tree}')
            with patch('deploy.environments.artifact.run_frontend_build') as build:
                with self.assertRaisesRegex(ValueError, 'artifact_commit_invalid'):
                    build_artifact(repo, tree, root / 'artifact')
                build.assert_not_called()

    def test_failed_run_is_never_selected_as_an_accepted_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=self.fake_build):
                build_artifact(repo, commit, output)
            (output / 'failure.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'artifact_failed_run'):
                verify_artifact(output)

    def test_source_archive_rejects_traversal_and_links(self):
        for name, kind in [('../outside.txt', tarfile.REGTYPE),
                           ('/outside.txt', tarfile.REGTYPE),
                           ('backend/link', tarfile.SYMTYPE),
                           ('backend/link', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / 'bad.tar'
                with tarfile.open(archive, 'w') as tar:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '../../outside'
                    member.size = 0
                    tar.addfile(member, io.BytesIO())
                with self.assertRaisesRegex(ValueError, 'artifact_archive_invalid'):
                    extract_source(archive, root / 'source')
                self.assertFalse((root / 'outside.txt').exists())

    def test_verifier_rejects_linked_manifest_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, commit = self.source_repo(root)
            output = root / 'artifact'
            with patch('deploy.environments.artifact.run_frontend_build', side_effect=self.fake_build):
                build_artifact(repo, commit, output)
            moved = root / 'manifest.json'
            (output / 'artifact.json').rename(moved)
            try:
                (output / 'artifact.json').symlink_to(moved)
            except OSError:
                self.skipTest('OS does not allow symlinks for this user')
            with self.assertRaisesRegex(ValueError, 'artifact_path_invalid'):
                verify_artifact(output)
            (output / 'artifact.json').unlink()
            moved.rename(output / 'artifact.json')
            alias = root / 'alias'
            alias.symlink_to(output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'artifact_path_invalid'):
                verify_artifact(alias)


if __name__ == '__main__':
    unittest.main()
