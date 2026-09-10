"""Build a backend image from an already verified immutable environment artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from .artifact import archive_contents, extract_source, file_hash, verify_artifact, write_json

IMAGE_FILES_SCRIPT = '''
import hashlib,json,pathlib
root=pathlib.Path('/app')
result={}
for path in root.rglob('*'):
    if path.is_symlink(): raise SystemExit(2)
    if path.is_file(): result[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(result))
'''


def image_files(image):
    result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                             '--memory', '128m', '--cpus', '0.5', '--pids-limit', '32',
                             '--entrypoint', 'python', image, '-c', IMAGE_FILES_SCRIPT],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('environment_image_content_check_failed')
    return json.loads(result.stdout)


def inspect_image(image):
    result = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('environment_image_inspect_failed')
    return json.loads(result.stdout)[0]


def docker_build(context, output, labels):
    image_file = output / 'docker-image-id'
    with (output / 'build.log').open('x', encoding='utf-8') as log:
        (output / 'build.log').chmod(0o600)
        result = subprocess.run(['docker', 'build', '--pull=false', '--label',
                             'org.opencontainers.image.revision=' + labels['org.opencontainers.image.revision'],
                             '--label', 'org.opencontainers.image.version=' + labels['org.opencontainers.image.version'],
                             '--label', 'binhu.artifact.id=' + labels['binhu.artifact.id'],
                             '--iidfile', str(image_file), str(context)], stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('environment_image_build_failed')
    return image_file.read_text().strip()


def _safe_root(path):
    path = Path(path).absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('image_path_invalid')
    return path


def build_image(artifact, expected_artifact_id, output):
    artifact = _safe_root(artifact)
    output = _safe_root(output)
    if output.exists():
        raise ValueError('image_output_exists')
    manifest = verify_artifact(artifact)
    if manifest['artifact_id'] != expected_artifact_id:
        raise ValueError('artifact_identity_mismatch')
    output.mkdir(parents=True, mode=0o700)
    try:
        source_archive = artifact / 'source.tar'
        context = output / 'context'
        context.mkdir()
        extract_source(source_archive, context)
        source = context
        if not (source / 'backend/Dockerfile').is_file():
            raise ValueError('image_source_invalid')
        shutil.copyfile(source / 'VERSION', source / 'backend/VERSION')
        dockerfile = source / 'backend/Dockerfile'
        with dockerfile.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write('\nCOPY VERSION /app/VERSION\nENV APP_VERSION=' + manifest['version'] + '\n')
        labels = {'org.opencontainers.image.revision': manifest['commit'],
                  'org.opencontainers.image.version': manifest['version'],
                  'binhu.artifact.id': manifest['artifact_id']}
        image_id = docker_build(source / 'backend', output, labels)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('image_identity_invalid')
        meta = {'schema': 1, 'artifact_id': manifest['artifact_id'], 'commit': manifest['commit'],
                'version': manifest['version'], 'image_id': image_id, 'labels': labels,
                'dockerfile_sha256': file_hash(dockerfile)}
        write_json(output / 'image.json', meta)
        return verify_image(artifact, expected_artifact_id, output)
    except Exception:
        write_json(output / 'failure.json', {'error': 'environment_image_build_failed'})
        raise RuntimeError('environment_image_build_failed') from None


def verify_image(artifact, expected_artifact_id, output):
    artifact = _safe_root(artifact)
    output = _safe_root(output)
    if (output / 'failure.json').exists():
        raise ValueError('image_failed_run')
    manifest = verify_artifact(artifact)
    if manifest['artifact_id'] != expected_artifact_id:
        raise ValueError('artifact_identity_mismatch')
    meta_file = output / 'image.json'
    if meta_file.is_symlink():
        raise ValueError('image_path_invalid')
    meta = json.loads(meta_file.read_text())
    expected_labels = {'binhu.artifact.id': expected_artifact_id,
                       'org.opencontainers.image.revision': manifest['commit'],
                       'org.opencontainers.image.version': manifest['version']}
    if (not isinstance(meta, dict)
            or set(meta) != {'schema', 'artifact_id', 'commit', 'version', 'image_id', 'labels', 'dockerfile_sha256'}
            or type(meta.get('schema')) is not int or meta['schema'] != 1
            or meta.get('artifact_id') != expected_artifact_id
            or meta.get('commit') != manifest['commit'] or meta.get('version') != manifest['version']
            or meta.get('labels') != expected_labels
            or not isinstance(meta.get('image_id'), str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', meta['image_id'])):
        raise ValueError('image_manifest_invalid')
    actual = inspect_image(meta.get('image_id', ''))
    labels = actual.get('Config', {}).get('Labels') or {}
    if (actual.get('Id') != meta.get('image_id') or labels.get('binhu.artifact.id') != expected_artifact_id
            or labels.get('org.opencontainers.image.revision') != manifest['commit']
            or labels.get('org.opencontainers.image.version') != manifest['version']
            or [entry for entry in actual.get('Config', {}).get('Env', [])
                if entry.startswith('APP_VERSION=')] != ['APP_VERSION=' + manifest['version']]):
        raise ValueError('image_identity_mismatch')
    source_files, _, _ = archive_contents(artifact / 'source.tar', source=True)
    expected_files = {name.removeprefix('backend/'): digest for name, digest in source_files.items()
                      if name.startswith('backend/')}
    with tarfile.open(artifact / 'source.tar') as archive:
        original_dockerfile = archive.extractfile('backend/Dockerfile').read().decode('utf-8')
    expected_dockerfile = (original_dockerfile + '\nCOPY VERSION /app/VERSION\nENV APP_VERSION='
                           + manifest['version'] + '\n').encode('utf-8')
    expected_files['Dockerfile'] = hashlib.sha256(expected_dockerfile).hexdigest()
    expected_files['VERSION'] = source_files['VERSION']
    if (meta.get('dockerfile_sha256') != expected_files['Dockerfile']
            or image_files(meta['image_id']) != expected_files):
        raise ValueError('image_content_mismatch')
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify'])
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--expected-artifact-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('image operations require the authorized Linux environment host')
    try:
        if args.action == 'build':
            docker_info = subprocess.run(['docker', 'info', '--format', '{{.DockerRootDir}}'],
                                         capture_output=True, text=True, timeout=20)
            if docker_info.returncode:
                raise RuntimeError('environment_docker_unavailable')
            memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            if (int(memory['MemAvailable'].split()[0]) < 2 * 1024 * 1024
                    or shutil.disk_usage(docker_info.stdout.strip()).free < 8 * 1024**3):
                raise ValueError('environment_build_resources_insufficient')
        operation = build_image if args.action == 'build' else verify_image
        result = operation(args.artifact, args.expected_artifact_id, args.output)
        print(json.dumps({key: result[key] for key in ('artifact_id', 'commit', 'version', 'image_id')}))
    except (ValueError, RuntimeError, OSError):
        raise SystemExit('environment_image_operation_failed; inspect private evidence') from None


if __name__ == '__main__':
    main()
