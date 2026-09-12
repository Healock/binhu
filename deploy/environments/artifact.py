"""Build immutable nonproduction frontend/source artifacts from a Git commit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

SHA = re.compile(r'[0-9a-f]{40}')
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?')


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('artifact_symlink_forbidden')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def extract_source(archive_path, target):
    """Git regular files only; reject link/path traversal before writing a file."""
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or not path.parts or ':' in member.name
                    or not (member.isdir() or member.isfile())):
                raise ValueError('artifact_archive_invalid')
            if path.parts[0] not in {'backend', 'frontend', 'VERSION', '.gitignore'}:
                raise ValueError('artifact_source_scope_invalid')
            if path.name in {'.env', '.env.local', '.env.environment', '.env.environment.local', '.npmrc'}:
                raise ValueError('artifact_private_config_forbidden')
            destination = target.joinpath(*path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def run_frontend_build(source, evidence):
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise RuntimeError('artifact_npm_unavailable')
    # No per-shell Vite override may bind an immutable package to another API.
    env = {key: value for key, value in os.environ.items() if not key.startswith('VITE_')}
    env['NODE_OPTIONS'] = '--max-old-space-size=8192'
    for step, args in [('dependencies', ['ci']), ('frontend', ['run', 'build', '--', '--mode', 'environment'])]:
        log = evidence / (step + '.log')
        with log.open('xb') as stream:
            log.chmod(0o600)
            result = subprocess.run([npm, *args], cwd=source / 'frontend', env=env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError('artifact_frontend_build_failed')


def build_artifact(repository, commit, output):
    if not SHA.fullmatch(commit):
        raise ValueError('artifact_commit_invalid')
    kind = subprocess.run(['git', '-C', str(repository), 'cat-file', '-t', commit], capture_output=True)
    if kind.returncode or kind.stdout.strip() != b'commit':
        raise ValueError('artifact_commit_invalid')
    output = Path(output).absolute()
    if output.exists():
        raise ValueError('artifact_output_exists')
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError('artifact_output_symlink')
    output.mkdir(parents=True, mode=0o700)
    output.chmod(0o700)
    try:
        archive = output / 'source.tar'
        result = subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar',
                                 '--output=' + str(archive), commit, 'VERSION', '.gitignore', 'backend', 'frontend'],
                                capture_output=True)
        if result.returncode:
            raise RuntimeError('artifact_source_export_failed')
        archive.chmod(0o600)
        source = output / 'source'
        source.mkdir()
        extract_source(archive, source)
        version = (source / 'VERSION').read_text().strip()
        if not VERSION.fullmatch(version):
            raise ValueError('artifact_version_invalid')
        backend = tree_hashes(source / 'backend')
        migrations = {name: digest for name, digest in backend.items()
                      if name.startswith('migrations/') or name in {'init.sql', 'database.py'}}
        run_frontend_build(source, output)
        static = source / 'frontend/dist-environment'
        if not (static / 'index.html').is_file():
            raise ValueError('artifact_frontend_missing')
        frontend = tree_hashes(static)
        with tarfile.open(output / 'frontend.tar', 'w') as archive:
            for name in frontend:
                path = static / name
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
        (output / 'frontend.tar').chmod(0o600)
        manifest = {
            'schema': 1, 'commit': commit, 'version': version,
            'build_mode': 'environment', 'ready_for_staging': False,
            'backend_tree_sha256': json_hash(backend), 'migration_sha256': json_hash(migrations),
            'frontend_files': frontend,
            'archives': {name: file_hash(output / name) for name in ('source.tar', 'frontend.tar')},
        }
        manifest['artifact_id'] = json_hash(manifest)
        write_json(output / 'artifact.json', manifest)
        return verify_artifact(output)
    except Exception:
        write_json(output / 'failure.json', {'error': 'artifact_build_failed', 'commit': commit})
        raise RuntimeError('artifact_build_failed; inspect private build evidence') from None


def verify_artifact(output):
    output = Path(output).absolute()
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError('artifact_path_invalid')
    for name in ('artifact.json', 'source.tar', 'frontend.tar'):
        if (output / name).is_symlink():
            raise ValueError('artifact_path_invalid')
    if (output / 'failure.json').exists():
        raise ValueError('artifact_failed_run')
    manifest = json.loads((output / 'artifact.json').read_text(encoding='utf-8'))
    required = {'schema', 'commit', 'version', 'build_mode', 'ready_for_staging',
                'backend_tree_sha256', 'migration_sha256', 'frontend_files', 'archives', 'artifact_id'}
    if (not isinstance(manifest, dict) or set(manifest) != required
            or not isinstance(manifest['commit'], str)
            or not isinstance(manifest['version'], str)
            or not VERSION.fullmatch(manifest['version'])
            or not isinstance(manifest['archives'], dict)
            or not isinstance(manifest['frontend_files'], dict)):
        raise ValueError('artifact_manifest_invalid')
    unsigned = {key: value for key, value in manifest.items() if key != 'artifact_id'}
    if (type(manifest['schema']) is not int or manifest['schema'] != 1 or not SHA.fullmatch(manifest['commit'])
            or manifest.get('build_mode') != 'environment' or manifest.get('ready_for_staging') is not False
            or set(manifest.get('archives', {})) != {'source.tar', 'frontend.tar'}
            or manifest.get('artifact_id') != json_hash(unsigned)):
        raise ValueError('artifact_manifest_invalid')
    for name, expected in manifest['archives'].items():
        path = output / name
        if path.is_symlink() or file_hash(path) != expected:
            raise ValueError('artifact_digest_mismatch')
    source, version, commit = archive_contents(output / 'source.tar', source=True)
    frontend, _, _ = archive_contents(output / 'frontend.tar', source=False)
    backend = {name.removeprefix('backend/'): digest for name, digest in source.items()
               if name.startswith('backend/')}
    migrations = {name: digest for name, digest in backend.items()
                  if name.startswith('migrations/') or name in {'init.sql', 'database.py'}}
    if (version != manifest['version'] or commit != manifest['commit']
            or frontend != manifest['frontend_files']
            or not {'VERSION', 'backend/Dockerfile', 'frontend/package.json',
                    'frontend/package-lock.json'}.issubset(source)
            or 'index.html' not in frontend
            or json_hash(backend) != manifest['backend_tree_sha256']
            or json_hash(migrations) != manifest['migration_sha256']):
        raise ValueError('artifact_content_mismatch')
    return manifest


def archive_contents(archive_path, *, source):
    """Inspect every member without extracting files or executing packaged code."""
    files, seen, version, commit = {}, set(), None, None
    try:
        with tarfile.open(archive_path) as archive:
            commit = archive.pax_headers.get('comment')
            for member in archive:
                path = PurePosixPath(member.name)
                name = path.as_posix()
                if (not path.parts or path.is_absolute() or '..' in path.parts
                        or '\\' in member.name or ':' in member.name
                        or name != member.name.rstrip('/') or name in seen
                        or not (member.isdir() or member.isfile())):
                    raise ValueError('artifact_archive_invalid')
                seen.add(name)
                if source and path.parts[0] not in {'backend', 'frontend', 'VERSION', '.gitignore'}:
                    raise ValueError('artifact_source_scope_invalid')
                if path.name in {'.env', '.env.local', '.env.environment', '.env.environment.local', '.npmrc'}:
                    raise ValueError('artifact_private_config_forbidden')
                if member.isdir():
                    continue
                with archive.extractfile(member) as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                    files[name] = digest.hexdigest()
                if source and name == 'VERSION':
                    if member.size > 128:
                        raise ValueError('artifact_version_invalid')
                    with archive.extractfile(member) as stream:
                        version = stream.read().decode('utf-8').strip()
    except (tarfile.TarError, UnicodeError):
        raise ValueError('artifact_archive_invalid') from None
    return files, version, commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify'])
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--commit')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'build':
            if not args.repository or not args.commit:
                parser.error('build requires repository and exact commit')
            result = build_artifact(args.repository, args.commit, args.output)
        else:
            result = verify_artifact(args.output)
        print(json.dumps({key: result[key] for key in ('artifact_id', 'commit', 'version', 'ready_for_staging')}))
    except (OSError, ValueError, RuntimeError):
        raise SystemExit('artifact_operation_failed; inspect private build evidence') from None


if __name__ == '__main__':
    main()
