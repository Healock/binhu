"""Build a backend image from an already verified immutable environment artifact."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from .artifact import verify_artifact, write_json


def inspect_image(image):
    result = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('environment_image_inspect_failed')
    return json.loads(result.stdout)[0]


def docker_build(context, output, labels):
    tag = 'binhu-environment-build:' + labels['binhu.artifact.id'][:16]
    result = subprocess.run(['docker', 'build', '--pull=false', '--label',
                             'org.opencontainers.image.revision=' + labels['org.opencontainers.image.revision'],
                             '--label', 'org.opencontainers.image.version=' + labels['org.opencontainers.image.version'],
                             '--label', 'binhu.artifact.id=' + labels['binhu.artifact.id'],
                             '--tag', tag, str(context)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('environment_image_build_failed')
    return inspect_image(tag)['Id']


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
        with tarfile.open(source_archive) as archive:
            archive.extractall(context, filter='data')
        source = context
        if not (source / 'backend/Dockerfile').is_file():
            raise ValueError('image_source_invalid')
        labels = {'org.opencontainers.image.revision': manifest['commit'],
                  'org.opencontainers.image.version': manifest['version'],
                  'binhu.artifact.id': manifest['artifact_id']}
        image_id = docker_build(source / 'backend', output, labels)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('image_identity_invalid')
        meta = {'schema': 1, 'artifact_id': manifest['artifact_id'], 'commit': manifest['commit'],
                'version': manifest['version'], 'image_id': image_id, 'labels': labels}
        write_json(output / 'image.json', meta)
        return meta
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
    meta = json.loads((output / 'image.json').read_text())
    if meta.get('artifact_id') != expected_artifact_id:
        raise ValueError('image_manifest_invalid')
    actual = inspect_image(meta.get('image_id', ''))
    labels = actual.get('Config', {}).get('Labels') or {}
    if (actual.get('Id') != meta.get('image_id') or labels.get('binhu.artifact.id') != expected_artifact_id
            or labels.get('org.opencontainers.image.revision') != manifest['commit']
            or labels.get('org.opencontainers.image.version') != manifest['version']):
        raise ValueError('image_identity_mismatch')
    return meta
