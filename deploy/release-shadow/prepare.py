"""Prepare a disposable release validation project on the Linux Docker host.

Run inside a NEW /srv/binhu-release-shadow-<run> directory. This does not
access production containers, publish ports, or start application schedulers.
Credentials stay in the project's private environment file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess


DOMAINS = (
    'OnlineData', 'OnlineDataArchive', 'daily_report', 'PlatformData',
    'VisitData', 'DispatchData', 'RegistryData', 'WorkflowData',
)
RUN_ID_PATTERN = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$')
PROJECT_PATTERN = re.compile(r'^binhu-release-shadow-[a-z0-9-]+$')


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def validate_run_id(run_id: str) -> str:
    """Validate the directory suffix before it becomes a SQL identifier."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError('run id must be 1-32 lowercase letters, digits, or hyphens')
    return run_id


def build_database_names(run_id: str) -> dict[str, str]:
    run_id = validate_run_id(run_id)
    prefix = 'ReleaseShadow_' + run_id.replace('-', '_')
    names = {domain: f'{prefix}_{index}' for index, domain in enumerate(DOMAINS)}
    if len(names) != 8 or len(set(names.values())) != 8:
        raise ValueError('shadow database names must be unique')
    if any(len(name) > 64 for name in names.values()):
        raise ValueError('shadow database name exceeds MySQL identifier limit')
    return names


def validate_bind_mounts(root: Path, *sources: Path) -> None:
    """Ensure every host path used by Compose stays in this run directory."""
    root_real = root.resolve()
    for source in sources:
        source_real = source.resolve(strict=False)
        if not source_real.is_relative_to(root_real):
            raise ValueError(f'bind mount escapes shadow project: {source}')


def validate_new_project(root: Path) -> None:
    """Reject stale state before creating any credentials or Docker resources."""
    if root.parent != Path('/srv') or not PROJECT_PATTERN.fullmatch(root.name):
        raise ValueError('must run inside a dedicated /srv/binhu-release-shadow-* directory')
    run_id = root.name.removeprefix('binhu-release-shadow-')
    validate_run_id(run_id)
    if root.is_symlink():
        raise ValueError('shadow project directory must not be a symlink')
    forbidden = {'.env', 'compose.json', 'init.sql', 'artifacts', 'validation'}
    if any((root / name).exists() for name in forbidden):
        raise ValueError('existing run state must not be reused')
    allowed = {'backend', 'prepare.py', 'verify.py', 'summary_verify.py', 'README.md'}
    unexpected = [item.name for item in root.iterdir() if item.name not in allowed]
    if unexpected:
        raise ValueError('shadow project directory contains unexpected existing files')
    backend = root / 'backend'
    if not (backend / 'init.sql').is_file():
        raise ValueError('release source missing')
    validate_bind_mounts(root, backend)


def docker_resource_exists(kind: str, name: str) -> bool:
    result = subprocess.run(
        ['docker', kind, 'inspect', name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def ensure_docker_names_available(project: str, volume_name: str, network_name: str) -> None:
    if docker_resource_exists('volume', volume_name):
        raise ValueError(f'run volume already exists: {volume_name}')
    if docker_resource_exists('network', network_name):
        raise ValueError(f'run network already exists: {network_name}')
    containers = run('docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}')
    if containers:
        raise ValueError(f'compose project already exists: {project}')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--mysql-image', required=True)
    parser.add_argument('--backend-image', required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        validate_new_project(root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for image in (args.mysql_image, args.backend_image):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise SystemExit('exact local image ID required')
        if run('docker', 'image', 'inspect', '--format', '{{.Id}}', image) != image:
            raise SystemExit('image identity mismatch')
    root.chmod(0o700)
    (root / 'artifacts').mkdir(exist_ok=True)
    run_id = root.name.removeprefix('binhu-release-shadow-')
    db_names = build_database_names(run_id)
    volume_name = f'{root.name}_data'
    network_name = f'{root.name}_isolated'
    try:
        ensure_docker_names_available(root.name, volume_name, network_name)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    root_password, db_password = secrets.token_hex(24), secrets.token_hex(24)
    db_user = 'release_shadow'
    sql = (root / 'backend/init.sql').read_text(encoding='utf-8')
    for name in sorted(DOMAINS, key=len, reverse=True):
        sql = re.sub(r'\b' + re.escape(name) + r'\b', db_names[name], sql)
    sql = sql.replace("'binhu'@'%'", f"'{db_user}'@'%'")
    sql += f"\nUSE `{db_names['OnlineData']}`;\n"
    sql += "CREATE TABLE _release_shadow_identity (run_id VARCHAR(100) PRIMARY KEY);\n"
    sql += f"INSERT INTO _release_shadow_identity VALUES ('{run_id}');\n"
    (root / 'init.sql').write_text(sql, encoding='utf-8')
    env = {
        'APP_ENVIRONMENT': 'shadow', 'LOAD_TEST_RUN_ID': run_id,
        'SESSION_COOKIE_NAME': 'binhu_shadow_session',
        'SESSION_COOKIE_SECURE': 'false', 'SESSION_COOKIE_SAMESITE': 'lax',
        'MYSQL_HOST': 'mysql', 'MYSQL_PORT': '3306', 'MYSQL_USER': db_user,
        'MYSQL_PASSWORD': db_password, 'MYSQL_POOL_SIZE': '4',
        'MYSQL_DOMAIN_DATABASES_ENABLED': 'true', 'PLATFORM_DOMAIN_ACTIVE': 'true',
        'VISIT_DOMAIN_ACTIVE': 'true', 'DISPATCH_DOMAIN_ACTIVE': 'true',
        'DAILY_DOMAIN_ACTIVE': 'true', 'REGISTRY_ADDRESS_DOMAIN_ACTIVE': 'true',
        'LOCAL_DATA_SOURCE_ENABLED': 'true', 'TXDOCS_ENABLED': 'false',
        'REALTIME_EVENTS_ENABLED': 'false', 'LOCAL_REPORT_SCHEDULER_ENABLED': 'false',
        'QMF_SOURCE_ACQUISITION_ENABLED': 'false', 'QMF_REGISTRATION_ENABLED': 'false',
        'VENUE_CLOUD_SYNC_ENABLED': 'false', 'VENUE_CLOUD_PULL_ENABLED': 'false',
        'CERTIFICATE_SOURCE_DAILY_ENABLED': 'false',
        'VISIT_SOURCE_BASE_URL': '', 'QMF_SOURCE_BASE_URL': '', 'PUBLIC_WEB_BASE_URL': '',
        'ENCRYPTION_KEY': secrets.token_hex(32),
        'BOOTSTRAP_ADMIN_USERNAME': 'release-observer@shadow',
        'BOOTSTRAP_ADMIN_PASSWORD': secrets.token_urlsafe(24),
    }
    for key, domain in zip(['ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM',
                             'VISIT', 'DISPATCH', 'REGISTRY', 'WORKFLOW'], DOMAINS):
        env[f'MYSQL_{key}_DB'] = db_names[domain]
    (root / '.env').write_text('\n'.join(f'{k}={v}' for k, v in env.items()) + '\n', encoding='utf-8')
    (root / '.env').chmod(0o600)
    labels = {'binhu.release-shadow.run': run_id}
    compose = {
        'name': root.name,
        'services': {
            'mysql': {
                'image': args.mysql_image, 'pull_policy': 'never',
                'environment': {'MYSQL_ROOT_PASSWORD': root_password,
                                'MYSQL_DATABASE': db_names['OnlineData'],
                                'MYSQL_USER': db_user, 'MYSQL_PASSWORD': db_password},
                'command': ['mysqld', '--max-connections=70', '--innodb-buffer-pool-size=128M'],
                'volumes': ['data:/var/lib/mysql', './init.sql:/docker-entrypoint-initdb.d/01-release.sql:ro'],
                'networks': ['isolated'], 'labels': labels, 'cpus': 1, 'mem_limit': '768m',
                'healthcheck': {'test': ['CMD', 'mysqladmin', '--protocol=tcp', '-h', '127.0.0.1', 'ping', '--silent'],
                                'interval': '3s', 'timeout': '2s', 'retries': 60},
            },
            'runner': {
                'image': args.backend_image, 'pull_policy': 'never',
                'profiles': ['validation'], 'env_file': ['.env'],
                'environment': {'PYTHONPATH': '/app'},
                'working_dir': '/app', 'entrypoint': ['python'],
                'command': ['-c', 'print("explicit validation command required")'],
                'volumes': ['./backend:/app:ro', './artifacts:/artifacts', './validation:/validation:ro'],
                'networks': ['isolated'], 'labels': labels, 'cpus': 1, 'mem_limit': '512m',
                'read_only': True, 'tmpfs': ['/tmp:size=128m'],
                'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
                'pids_limit': 128,
            },
        },
        'networks': {'isolated': {'internal': True, 'labels': labels}},
        'volumes': {'data': {'name': volume_name, 'labels': labels}},
    }
    (root / 'compose.json').write_text(json.dumps(compose, indent=2), encoding='utf-8')
    (root / 'compose.json').chmod(0o600)
    validation_dir = root / 'validation'
    validation_dir.mkdir()
    verification_source = Path(__file__).resolve().with_name('verify.py')
    if not verification_source.is_file():
        raise SystemExit('validation/verify.py source missing')
    shutil.copyfile(verification_source, validation_dir / 'verify.py')
    (validation_dir / 'verify.py').chmod(0o500)
    summary_verification_source = Path(__file__).resolve().with_name('summary_verify.py')
    if not summary_verification_source.is_file():
        raise SystemExit('validation/summary_verify.py source missing')
    shutil.copyfile(summary_verification_source, validation_dir / 'summary_verify.py')
    (validation_dir / 'summary_verify.py').chmod(0o500)
    validate_bind_mounts(root, root / 'backend', root / 'artifacts', validation_dir)
    (root / 'artifacts/identity.json').write_text(json.dumps({
        'project': root.name, 'run_id': run_id, 'databases': db_names,
        'mysql_image': args.mysql_image, 'runner_image': args.backend_image,
        'network_internal': True, 'published_ports': [],
        'backend_sha256': sha256_tree(root / 'backend'),
        'init_sql_sha256': sha256_file(root / 'init.sql'),
    }, indent=2), encoding='utf-8')
    print(json.dumps({'project': root.name, 'status': 'prepared', 'started': False}))


if __name__ == '__main__':
    main()
