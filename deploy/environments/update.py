"""Validate an existing environment and prepare a paired application update."""
from __future__ import annotations

import copy
import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

from .artifact import file_hash, tree_hashes, verify_artifact, write_json
from .dev_bootstrap_acceptance import verify as verify_dev_bootstrap
from .image import verify_image
from .runtime import DOMAINS, KEYS, SPEC, root_for

EVIDENCE_ROOT = Path('/srv/deploy-backups/environment-triad')


def parse_environment(text):
    values = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch('[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError('environment_configuration_invalid')
        values[key] = value
    return values


def candidate_configuration(environment, root, manifest, compose, env_text, image):
    if environment not in SPEC:
        raise ValueError('nonproduction_environment_required')
    prefix, _, port, cookie = SPEC[environment]
    project = 'binhu-' + environment
    if (manifest.get('environment') != environment or manifest.get('project') != project
            or manifest.get('port') != port or compose.get('name') != project):
        raise ValueError('environment_identity_mismatch')
    values = parse_environment(env_text)
    if (values.get('APP_ENVIRONMENT') != environment or values.get('SESSION_COOKIE_NAME') != cookie
            or values.get('MYSQL_HOST') != 'environment-mysql'
            or values.get('MYSQL_USER') != 'environment_app'
            or values.get('MYSQL_PORT', '3306') != '3306'
            or values.get('LOCAL_DATA_SOURCE_ENABLED') != 'true'):
        raise ValueError('environment_identity_mismatch')
    for key in ('TXDOCS_ENABLED', 'QMF_SOURCE_ACQUISITION_ENABLED', 'QMF_REGISTRATION_ENABLED',
                'VENUE_CLOUD_SYNC_ENABLED', 'VENUE_CLOUD_PULL_ENABLED',
                'CERTIFICATE_SOURCE_DAILY_ENABLED', 'LOCAL_REPORT_SCHEDULER_ENABLED'):
        if values.get(key) != 'false':
            raise ValueError('environment_external_access_enabled')
    databases = [values.get('MYSQL_' + key + '_DB', '') for key in KEYS]
    if (len(set(databases)) != len(DOMAINS)
            or any(not re.fullmatch(prefix + '[A-Za-z0-9_]+', name) for name in databases)
            or manifest.get('databases') != dict(zip(DOMAINS, databases))):
        raise ValueError('environment_database_mismatch')
    if set(compose.get('services', {})) != {'backend', 'environment-mysql', 'redis'}:
        raise ValueError('environment_services_unreviewed')
    networks = compose.get('networks', {})
    if (set(networks) != {'internal'} or networks['internal'].get('name') != project + '_internal'
            or networks['internal'].get('external')):
        raise ValueError('environment_network_mismatch')
    if (set(compose.get('volumes', {})) != {'mysql', 'redis'}
            or any(compose['volumes'][name].get('name') != project + '_' + name
                   or compose['volumes'][name].get('external') for name in ('mysql', 'redis'))):
        raise ValueError('environment_volume_mismatch')
    for name, service in compose['services'].items():
        if (service.get('networks') != ['internal'] or service.get('network_mode')
                or service.get('privileged') or service.get('build')
                or service.get('labels', {}).get('binhu.environment') != environment
                or not service.get('mem_limit') or not service.get('cpus')
                or not service.get('logging', {}).get('options', {}).get('max-size')):
            raise ValueError('environment_service_isolation_invalid')
        if service.get('image') != manifest.get('images', {}).get(name if name != 'environment-mysql' else 'mysql'):
            raise ValueError('environment_image_drift')
    backend = compose['services']['backend']
    if (backend.get('ports') != [f'127.0.0.1:{port}:37125'] or backend.get('env_file') != ['backend.env']
            or backend.get('environment') or backend.get('cap_drop') != ['ALL']
            or backend.get('security_opt') != ['no-new-privileges:true']):
        raise ValueError('environment_backend_override_invalid')
    if backend.get('volumes') != ['./static:/app/static:ro']:
        previous = manifest.get('artifact_id', '')
        if (not re.fullmatch('[0-9a-f]{64}', previous)
                or backend.get('volumes') != [f'./releases/{previous}/static:/app/static:ro']):
            raise ValueError('environment_static_mount_unreviewed')
    if (not re.fullmatch('sha256:[0-9a-f]{64}', image.get('image_id', ''))
            or not re.fullmatch('[0-9a-f]{64}', image.get('artifact_id', ''))):
        raise ValueError('environment_candidate_invalid')
    candidate = copy.deepcopy(compose)
    candidate['services']['backend']['image'] = image['image_id']
    candidate['services']['backend']['volumes'] = [f"./releases/{image['artifact_id']}/static:/app/static:ro"]
    values['APP_VERSION'] = image['version']
    new_manifest = copy.deepcopy(manifest)
    new_manifest.update({'version': image['version'], 'commit': image['commit'], 'artifact_id': image['artifact_id']})
    new_manifest['images']['backend'] = image['image_id']
    return candidate, '\n'.join(key + '=' + value for key, value in values.items()) + '\n', new_manifest


def read_configuration(root):
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    if set(manifest.get('hashes', {})) != {'compose.json', 'backend.env', 'init.sql'}:
        raise ValueError('environment_configuration_hashes_missing')
    for name, expected in manifest['hashes'].items():
        if (root / name).is_symlink() or file_hash(root / name) != expected:
            raise ValueError('environment_configuration_drift')
    return manifest, json.loads((root / 'compose.json').read_text()), (root / 'backend.env').read_text()


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('environment_update_command_failed')
    return result.stdout


def container_baseline():
    ids = command(['docker', 'ps', '-aq']).split()
    containers = json.loads(command(['docker', 'inspect', *ids]))
    return {container['Id']: {'name': container['Name'], 'image': container['Image'],
                             'started': container['State']['StartedAt'],
                             'running': container['State']['Running'], 'restarts': container['RestartCount']}
            for container in containers if not container['Name'].startswith('/binhu-development-backend-')}


def verify_baseline(before):
    current = container_baseline()
    if any(current.get(key) != value for key, value in before.items()):
        raise ValueError('environment_other_container_changed')


def backup_databases(root, manifest, evidence):
    compose = json.loads((root / 'compose.json').read_text())
    service = compose['services']['environment-mysql']
    databases = list(manifest['databases'].values())
    if any(not re.fullmatch('Dev_[A-Za-z0-9_]+', name) for name in databases):
        raise ValueError('development_backup_target_invalid')
    container = command(['docker', 'compose', '-f', str(root / 'compose.json'),
                         'ps', '-q', 'environment-mysql']).strip()
    if not re.fullmatch('[0-9a-f]{64}', container):
        raise ValueError('development_database_container_invalid')
    backup = evidence / 'development-databases.sql.gz'
    arguments = ['docker', 'exec', container, 'sh', '-c',
                 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump -u root '
                 '--single-transaction --routines --triggers --events --hex-blob '
                 '--set-gtid-purged=OFF --databases "$@"', 'sh', *databases]
    with (evidence / 'backup-errors.log').open('xb') as errors, gzip.open(backup, 'xb') as destination:
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=errors)
        try:
            shutil.copyfileobj(process.stdout, destination)
            if process.wait(timeout=300):
                raise RuntimeError('development_database_backup_failed')
        finally:
            process.stdout.close()
    size = 0
    with gzip.open(backup, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
    if size < 1024:
        raise ValueError('development_database_backup_empty')
    write_json(evidence / 'backup.json', {'sha256': file_hash(backup), 'bytes': backup.stat().st_size,
                                        'uncompressed_bytes': size, 'databases': databases,
                                        'mysql_image': service['image']})


def health(environment, version):
    port = SPEC[environment][2]
    if environment == 'development':
        bootstrap = verify_dev_bootstrap(version)
    else:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/app/bootstrap', timeout=8) as response:
            payload = json.load(response)
        if (payload.get('environment') != environment or payload.get('server_version') != version
                or payload.get('api_entry') != '/' + SPEC[environment][1] + '/api'):
            raise ValueError('environment_bootstrap_mismatch')
        bootstrap = {
            'environment': environment,
            'server_version': version,
            'api_entry': '/' + SPEC[environment][1] + '/api',
        }
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=8) as response:
        if response.status != 200:
            raise ValueError('environment_health_failed')
    return {'environment': environment, 'version': version, 'health': True, 'bootstrap': bootstrap}


def wait_healthy(environment, version):
    for attempt in range(45):
        try:
            return health(environment, version)
        except (OSError, ValueError):
            if attempt < 44:
                time.sleep(2)
    raise ValueError('environment_candidate_health_failed')


def replace_file(source, destination):
    temporary = destination.with_name(destination.name + '.candidate')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('environment_previous_update_unresolved')
    with temporary.open('xb') as stream:
        stream.write(source.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, destination)


def check_resources(root):
    available = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    if int(available['MemAvailable'].split()[0]) < 1536 * 1024 or shutil.disk_usage(root).free < 4 * 1024**3:
        raise ValueError('environment_update_resources_insufficient')


def apply_development(artifact, image_directory, expected_id, evidence):
    import fcntl
    from .database_identity import run as verify_database_identity
    root = root_for('development')
    evidence = Path(evidence).absolute()
    evidence_parent = EVIDENCE_ROOT
    if (evidence.parent != evidence_parent or evidence.resolve() != evidence or evidence.exists()
            or not re.fullmatch('dev-update-[0-9a-f]{16}', evidence.name)):
        raise ValueError('environment_evidence_path_invalid')
    with (root / '.deployment.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = verify_image(artifact, expected_id, image_directory)
        artifact_manifest = verify_artifact(artifact)
        manifest, compose, env_text = read_configuration(root)
        candidate, new_env, new_manifest = candidate_configuration(
            'development', root, manifest, compose, env_text, image)
        if not verify_database_identity('development')['all_markers_present']:
            raise ValueError('environment_database_identity_incomplete')
        baseline = container_baseline()
        check_resources(root)
        evidence.mkdir(mode=0o700)
        changed = False
        try:
            write_json(evidence / 'before-containers.json', baseline)
            previous = evidence / 'previous'
            previous.mkdir()
            for name in ('compose.json', 'backend.env', 'manifest.json', 'init.sql'):
                shutil.copyfile(root / name, previous / name)
            static_source = root / compose['services']['backend']['volumes'][0].split(':')[0]
            old_static_hashes = tree_hashes(static_source)
            shutil.copytree(static_source, previous / 'static')
            if tree_hashes(previous / 'static') != old_static_hashes:
                raise ValueError('environment_static_backup_failed')
            write_json(evidence / 'previous-files.json', tree_hashes(previous))
            backup_databases(root, manifest, evidence)
            releases = root / 'releases'
            if releases.is_symlink():
                raise ValueError('environment_release_path_invalid')
            releases.mkdir(exist_ok=True)
            release = releases / expected_id
            release.mkdir()
            static = release / 'static'
            static.mkdir()
            with tarfile.open(Path(artifact) / 'frontend.tar') as archive:
                archive.extractall(static, filter='data')
            if tree_hashes(static) != artifact_manifest['frontend_files']:
                raise ValueError('environment_static_content_mismatch')
            prepared = evidence / 'candidate'
            prepared.mkdir()
            write_json(prepared / 'compose.json', candidate)
            (prepared / 'backend.env').write_text(new_env, encoding='utf-8', newline='\n')
            new_manifest['hashes'] = {'compose.json': file_hash(prepared / 'compose.json'),
                                      'backend.env': file_hash(prepared / 'backend.env'),
                                      'init.sql': file_hash(root / 'init.sql')}
            write_json(prepared / 'manifest.json', new_manifest)
            command(['docker', 'compose', '--project-directory', str(root), '-f',
                     str(prepared / 'compose.json'), 'config', '--quiet'])
            if read_configuration(root) != (manifest, compose, env_text):
                raise ValueError('environment_configuration_drift')
            changed = True
            for name in ('backend.env', 'compose.json', 'manifest.json'):
                replace_file(prepared / name, root / name)
            command(['docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d', '--no-deps', 'backend'])
            report = wait_healthy('development', image['version'])
            if not verify_database_identity('development')['all_markers_present']:
                raise ValueError('environment_database_identity_incomplete')
            backend_id = command(['docker', 'compose', '-f', str(root / 'compose.json'), 'ps', '-q', 'backend']).strip()
            backend = json.loads(command(['docker', 'inspect', backend_id]))[0]
            if (backend['Image'] != image['image_id']
                    or backend['Config']['Labels'].get('com.docker.compose.project') != 'binhu-development'
                    or [entry for entry in backend['Config']['Env'] if entry.startswith('APP_VERSION=')]
                    != ['APP_VERSION=' + image['version']]):
                raise ValueError('environment_running_image_mismatch')
            verify_baseline(baseline)
            report.update({'artifact_id': expected_id, 'image_id': image['image_id'],
                           'commit': image['commit'], 'business_acceptance': False})
            write_json(evidence / 'result.json', report)
            return report
        except Exception:
            rollback = 'not_required'
            if changed:
                try:
                    for name in ('backend.env', 'compose.json', 'manifest.json'):
                        replace_file(previous / name, root / name)
                    command(['docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d', '--no-deps', 'backend'])
                    rollback = 'application_restored_health_pending'
                    old_version = parse_environment(env_text).get('APP_VERSION') or manifest.get('version') or '0.0.0'
                    wait_healthy('development', old_version)
                    rollback = 'application_restored_health_verified'
                except Exception:
                    rollback = 'application_restore_failed'
            write_json(evidence / 'failure.json', {'error': 'development_update_failed',
                                                  'rollback': rollback, 'database_restored': False})
            raise RuntimeError('development_update_failed; inspect private evidence') from None


def measure_development(artifact, image_directory, expected_id):
    from .database_identity import run as verify_database_identity
    root = root_for('development')
    image = verify_image(artifact, expected_id, image_directory)
    manifest, compose, env_text = read_configuration(root)
    candidate_configuration('development', root, manifest, compose, env_text, image)
    if not verify_database_identity('development')['all_markers_present']:
        raise ValueError('environment_database_identity_incomplete')
    check_resources(root)
    return {'environment': 'development', 'candidate_version': image['version'],
            'artifact_id': expected_id, 'image_id': image['image_id'],
            'database_identity_verified': True, 'configuration_verified': True,
            'configuration_hashes': manifest['hashes'], 'application_switched': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['measure-development', 'apply-development'])
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--image-directory', type=Path, required=True)
    parser.add_argument('--expected-artifact-id', required=True)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('run on authorized Linux environment host as root')
    os.umask(0o077)
    try:
        if args.action == 'measure-development':
            report = measure_development(args.artifact, args.image_directory, args.expected_artifact_id)
        else:
            if not args.evidence:
                parser.error('apply requires a new evidence directory')
            report = apply_development(args.artifact, args.image_directory, args.expected_artifact_id, args.evidence)
        print(json.dumps(report))
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.TimeoutExpired):
        raise SystemExit('environment_update_failed; inspect private evidence') from None


if __name__ == '__main__':
    main()
