"""Provision NEW isolated environments; never inspect/copy production secrets.

Use immutable local image IDs and separately built frontend artifacts. Repeated
prepare is rejected. apply accepts only the manifest/config produced by prepare.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

DOMAINS = ('OnlineData', 'OnlineDataArchive', 'daily_report', 'PlatformData', 'VisitData', 'DispatchData', 'RegistryData', 'WorkflowData')
KEYS = ('ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM', 'VISIT', 'DISPATCH', 'REGISTRY', 'WORKFLOW')
SPEC = {'development': ('Dev_', 'dev', 48125, 'binhu_dev_session'), 'staging': ('Staging_', 'staging', 48126, 'binhu_staging_session')}


def command(*args):
    # Do not expose exception arguments or Docker's expanded secret configuration.
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('environment command failed; inspect private server diagnostics')
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def root_for(environment):
    root = Path('/srv/binhu-environments') / environment
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('environment root must not be a symlink')
    if root.resolve() != root:
        raise ValueError('unexpected environment root')
    return root


def image_id(value):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value or ''):
        raise ValueError('immutable local image ID required')
    if command('docker', 'image', 'inspect', '--format', '{{.Id}}', value) != value:
        raise ValueError('image identity mismatch')
    return value


def private_file(path, value):
    path.write_text(value, encoding='utf-8')
    path.chmod(0o600)


def prepare(args):
    root = root_for(args.environment)
    if root.exists():
        raise ValueError('existing environment must be verified, never reinitialized')
    images = {name: image_id(getattr(args, name + '_image')) for name in ('backend', 'mysql', 'redis')}
    prefix, suffix, port, cookie = SPEC[args.environment]
    source = Path(args.source).resolve()
    static = Path(args.static).resolve()
    if not (source / 'backend/init.sql').is_file() or not (static / 'index.html').is_file():
        raise ValueError('source and compiled frontend required')
    # The backend serves the same immutable static bundle for all same-origin
    # prefixes; runtime API resolution supplies the environment path.
    if '/assets/' not in (static / 'index.html').read_text(encoding='utf-8'):
        raise ValueError('compiled frontend assets required')
    project = f'binhu-{args.environment}'
    if command('docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'):
        raise ValueError('compose project already exists')
    for kind in ('volume', 'network'):
        for name in command('docker', kind, 'ls', '--format', '{{.Name}}').splitlines():
            if name.startswith(project + '_'):
                raise ValueError('existing resources require review')
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    db_password, root_password, redis_password = [secrets.token_hex(24) for _ in range(3)]
    salt = secrets.token_bytes(32)
    username = 'observer@' + suffix
    initial_password = 'Init-' + hmac.new(salt, username.encode(), hashlib.sha256).hexdigest()[:24] + '!'
    dbs = {name: prefix + name for name in DOMAINS}
    env = {
        'APP_ENVIRONMENT': args.environment, 'SESSION_COOKIE_NAME': cookie,
        'SESSION_COOKIE_SECURE': 'true', 'SESSION_COOKIE_SAMESITE': 'lax',
        'MYSQL_HOST': 'environment-mysql', 'MYSQL_USER': 'environment_app',
        'MYSQL_PASSWORD': db_password, 'MYSQL_POOL_SIZE': '4',
        'MYSQL_DOMAIN_DATABASES_ENABLED': 'true', 'LOCAL_DATA_SOURCE_ENABLED': 'true',
        'TXDOCS_ENABLED': 'false', 'QMF_SOURCE_ACQUISITION_ENABLED': 'false',
        'QMF_REGISTRATION_ENABLED': 'false', 'VENUE_CLOUD_SYNC_ENABLED': 'false',
        'VENUE_CLOUD_PULL_ENABLED': 'false', 'CERTIFICATE_SOURCE_DAILY_ENABLED': 'false',
        'QMF_SOURCE_BASE_URL': '', 'VISIT_SOURCE_BASE_URL': '', 'QMF_API_BASE_URL': '',
        'LOCAL_REPORT_SCHEDULER_ENABLED': 'false', 'REALTIME_EVENTS_ENABLED': 'false',
        'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'REDIS_URL': f'redis://:{redis_password}@redis:6379/0',
        'BOOTSTRAP_ADMIN_USERNAME': username, 'BOOTSTRAP_ADMIN_PASSWORD': initial_password,
        'STATIC_DIR': '/app/static', 'OPS_AGENT_URL': '', 'OPS_AGENT_TOKEN': '',
    }
    for flag in ('PLATFORM_DOMAIN_ACTIVE', 'VISIT_DOMAIN_ACTIVE', 'DISPATCH_DOMAIN_ACTIVE', 'DAILY_DOMAIN_ACTIVE', 'REGISTRY_ADDRESS_DOMAIN_ACTIVE', 'REGISTRY_FEATURE_ENABLED', 'WORKFLOW_FEATURE_ENABLED'):
        env[flag] = 'true'
    env.update({f'MYSQL_{key}_DB': dbs[domain] for key, domain in zip(KEYS, DOMAINS)})
    sql = (source / 'backend/init.sql').read_text(encoding='utf-8')
    for domain in sorted(DOMAINS, key=len, reverse=True):
        sql = re.sub(r'\b' + re.escape(domain) + r'\b', dbs[domain], sql)
    sql = sql.replace("'binhu'@'%'", "'environment_app'@'%'")
    sql += f"\nUSE `{dbs['OnlineData']}`;\nCREATE TABLE _environment_identity (id INT PRIMARY KEY, environment VARCHAR(32) NOT NULL);\nINSERT INTO _environment_identity VALUES (1, '{args.environment}');\n"
    private_file(root / 'init.sql', sql)
    # Contains schema only. MySQL's unprivileged entrypoint must be able to read it.
    (root / 'init.sql').chmod(0o644)
    private_file(root / 'backend.env', '\n'.join(f'{k}={v}' for k, v in env.items()) + '\n')
    private_file(root / 'initial-account.json', json.dumps({'username': username, 'initial_password': initial_password, 'salt': salt.hex()}))
    shutil.copytree(static, root / 'static')
    labels = {'binhu.environment': args.environment}
    common = {'networks': ['internal'], 'labels': labels, 'restart': 'unless-stopped', 'pull_policy': 'never',
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}, 'pids_limit': 256}
    compose = {'name': project, 'services': {
        'environment-mysql': {**common, 'image': images['mysql'], 'cpus': 2, 'mem_limit': '1536m',
            'environment': {'MYSQL_ROOT_PASSWORD': root_password, 'MYSQL_DATABASE': dbs['OnlineData'], 'MYSQL_USER': 'environment_app', 'MYSQL_PASSWORD': db_password},
            'command': ['mysqld', '--max-connections=80', '--innodb-buffer-pool-size=256M'],
            'volumes': ['mysql:/var/lib/mysql', './init.sql:/docker-entrypoint-initdb.d/01-environment.sql:ro'],
            'healthcheck': {'test': ['CMD', 'mysqladmin', 'ping', '-h', '127.0.0.1'], 'interval': '5s', 'timeout': '3s', 'retries': 60}},
        'redis': {**common, 'image': images['redis'], 'cpus': 0.5, 'mem_limit': '192m',
            'command': ['redis-server', '--requirepass', redis_password, '--maxmemory', '96mb', '--maxmemory-policy', 'noeviction', '--appendonly', 'yes'], 'volumes': ['redis:/data']},
        'backend': {**common, 'image': images['backend'], 'cpus': 2, 'mem_limit': '768m',
            'env_file': ['backend.env'], 'ports': [f'127.0.0.1:{port}:37125'],
            'volumes': ['./static:/app/static:ro'], 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'depends_on': {'environment-mysql': {'condition': 'service_healthy'}}},
    }, 'networks': {'internal': {'name': project + '_internal', 'internal': False, 'labels': labels}},
       'volumes': {key: {'name': project + '_' + key, 'labels': labels} for key in ('mysql', 'redis')}}
    private_file(root / 'compose.json', json.dumps(compose, indent=2))
    private_file(root / 'manifest.json', json.dumps({'environment': args.environment, 'project': project,
        'port': port, 'images': images, 'databases': dbs,
        'hashes': {p: digest(root / p) for p in ('compose.json', 'backend.env', 'init.sql')}}, indent=2))
    print(json.dumps({'environment': args.environment, 'prepared': True, 'started': False}))


def apply(environment):
    root = root_for(environment)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest['environment'] != environment or manifest['project'] != f'binhu-{environment}':
        raise ValueError('manifest identity mismatch')
    if any(digest(root / name) != expected for name, expected in manifest['hashes'].items()):
        raise ValueError('prepared configuration was changed')
    for image in manifest['images'].values():
        image_id(image)
    command('docker', 'compose', '-f', str(root / 'compose.json'), 'config', '--quiet')
    command('docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d')
    print(json.dumps({'environment': environment, 'started': True, 'acceptance': 'pending'}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply'])
    parser.add_argument('--environment', choices=SPEC, required=True)
    parser.add_argument('--source')
    parser.add_argument('--static')
    for name in ('backend', 'mysql', 'redis'):
        parser.add_argument(f'--{name}-image')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('run on authorized Linux environment host as root')
    try:
        prepare(args) if args.action == 'prepare' else apply(args.environment)
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
