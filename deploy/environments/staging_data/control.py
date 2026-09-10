"""Server-only read-only measurement/export; no import or live database switch."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from .codec import SnapshotError

ROOT = Path('/srv/deploy-backups/environment-triad/staging-snapshots')
MODULES = ('codec', 'registry', 'tasks', 'fences', 'organization', 'relations', 'digests', 'build')


def safe_directory(path, *, create=False):
    if not path.is_absolute() or path != path.resolve():
        raise SnapshotError('evidence_path_invalid')
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise SnapshotError('evidence_symlink_rejected')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise SnapshotError('evidence_permissions_invalid')


def private_json(path, value):
    # Exclusive creation, including failure evidence, prevents failed runs
    # being overwritten by a later successful probe.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def source_program(snapshot_id, salt, *, measure, exclude_orphan_property_links=False):
    modules = {name: (Path(__file__).parent / (name + '.py')).read_text(encoding='utf-8') for name in MODULES}
    # Load reviewed pure modules in memory. No code or business file is written
    # into the production container and its app process is not changed.
    program = "import sys,types,json,asyncio\n"
    program += "package=types.ModuleType('snapshot_tool'); package.__path__=[]; sys.modules['snapshot_tool']=package\n"
    program += 'sources=' + repr(modules) + '\n'
    program += "for name,source in sources.items():\n    module=types.ModuleType('snapshot_tool.'+name); module.__package__='snapshot_tool'; sys.modules[module.__name__]=module; exec(compile(source, '<snapshot_tool.'+name+'>', 'exec'), module.__dict__)\n"
    program += 'snapshot_id=' + repr(snapshot_id) + '\nsalt=bytes.fromhex(' + repr(salt.hex()) + ')\n'
    program += 'measure=' + repr(measure) + '\n'
    program += 'exclude_orphan_property_links=' + repr(exclude_orphan_property_links) + '\n'
    program += '''
from snapshot_tool.build import build,source_settings
from snapshot_tool.codec import SnapshotError
from config import settings
import aiomysql
async def main():
    source_settings(settings)
    conn=await aiomysql.connect(host=settings.MYSQL_HOST,port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,password=settings.MYSQL_PASSWORD,charset='utf8mb4',
        connect_timeout=5,autocommit=False)
    try:
        result=await build(conn,snapshot_id,salt,settings=settings,
            exclude_orphan_property_links=exclude_orphan_property_links)
        print(json.dumps({'ok':True,'result':{'report':result['report']} if measure else result},ensure_ascii=True))
    finally:
        conn.close()
try:
    asyncio.run(main())
except SnapshotError as exc:
    print(json.dumps({'ok':False,'reason':str(exc)}))
except Exception:
    print(json.dumps({'ok':False,'reason':'snapshot_source_operation_failed'}))
'''
    hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in modules.items()}
    return program, hashes


def preflight():
    if os.name != 'posix' or os.geteuid() != 0:
        raise SnapshotError('server_root_required')
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    if int(memory['MemAvailable'].split()[0]) < 3 * 1024**2:
        raise SnapshotError('insufficient_memory_for_snapshot')
    for path in (ROOT, Path('/data/docker')):
        stats = os.statvfs(path)
        if stats.f_bavail * stats.f_frsize < 10 * 1024**3:
            raise SnapshotError('insufficient_snapshot_disk')
    result = subprocess.run(['docker', 'inspect', 'binhu-backend'], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise SnapshotError('source_container_unavailable')
    container, = json.loads(result.stdout)
    labels = container['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.project') != 'binhu' or labels.get('com.docker.compose.service') != 'backend':
        raise SnapshotError('source_container_identity_mismatch')
    if not container['State']['Running'] or container['State']['OOMKilled']:
        raise SnapshotError('source_container_unhealthy')
    environment = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
    if environment.get('APP_ENVIRONMENT') != 'production':
        raise SnapshotError('source_environment_mismatch')
    return {'container_id': container['Id'], 'started_at': container['State']['StartedAt'],
            'restart_count': container['RestartCount'], 'memory_available_kib': int(memory['MemAvailable'].split()[0])}


def execute(action, *, exclude_orphan_property_links=False):
    import fcntl
    os.umask(0o077)
    safe_directory(ROOT, create=True)
    with (ROOT / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = preflight()
        snapshot_id = 'staging-' + secrets.token_hex(8)
        path = ROOT / snapshot_id
        path.mkdir(mode=0o700)
        safe_directory(path)
        private_json(path / 'before.json', before)
        program, hashes = source_program(snapshot_id, secrets.token_bytes(32), measure=action == 'measure',
            exclude_orphan_property_links=exclude_orphan_property_links)
        private_json(path / 'policy.json', {'exclude_orphan_property_links':exclude_orphan_property_links,
            'maximum_excluded_links':3 if exclude_orphan_property_links else 0})
        private_json(path / 'code-hashes.json', hashes)
        started = time.monotonic()
        try:
            response = subprocess.run(['docker', 'exec', '-i', before['container_id'], 'python', '-'],
                input=program, capture_output=True, text=True, timeout=300)
            # Never copy stdout/stderr to diagnostics: a driver error may
            # contain SQL parameters. The inner program emits a fixed envelope.
            lines = [line for line in response.stdout.splitlines() if line.startswith('{"ok":')]
            if response.returncode or len(lines) != 1:
                raise SnapshotError('snapshot_reader_failed')
            envelope = json.loads(lines[0])
            if not envelope.get('ok'):
                code = envelope.get('reason', '')
                raise SnapshotError(code if re.fullmatch('[a-z_]{1,100}', code) else 'snapshot_reader_failed')
            result = envelope['result']
            after = preflight()
            if any(after[k] != before[k] for k in ('container_id', 'started_at', 'restart_count')):
                raise SnapshotError('production_baseline_changed')
            private_json(path / 'after.json', after)
            private_json(path / 'report.json', result['report'])
            if action == 'export':
                private_json(path / 'snapshot.json', result)
                private_json(path / 'snapshot-sha256.json', {'sha256': hashlib.sha256((path / 'snapshot.json').read_bytes()).hexdigest()})
            private_json(path / 'operation.json', {'action': action, 'success': True,
                'elapsed_seconds': round(time.monotonic()-started, 3), 'snapshot_id': snapshot_id})
            return {'snapshot_id': snapshot_id, 'action': action, **result['report']}
        except Exception as exc:
            code = str(exc) if isinstance(exc, SnapshotError) else 'snapshot_operation_failed'
            private_json(path / 'failure.json', {'reason': code, 'snapshot_id': snapshot_id})
            raise SnapshotError(code) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('measure', 'export'))
    parser.add_argument('--exclude-orphan-property-links', action='store_true',
        help='Explicitly reject up to three nonconfirmed orphan relations; preserve houses and report each rejection')
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args.action,exclude_orphan_property_links=args.exclude_orphan_property_links)))
    except (SnapshotError, OSError):
        raise SystemExit('snapshot preparation failed; inspect private fixed-code evidence') from None


if __name__ == '__main__':
    main()
