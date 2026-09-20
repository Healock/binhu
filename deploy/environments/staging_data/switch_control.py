"""Atomically switch only the Staging backend to a verified snapshot."""
from __future__ import annotations

from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request

from .apply_control import execute as candidate_execute
from .codec import SnapshotError
from .control import ROOT, private_json, safe_directory
from .target import DOMAINS, KEYS, database_names
from ..artifact import file_hash
from ..runtime import root_for
from ..update import backup_databases, command, health, parse_environment, read_configuration, replace_file


EVIDENCE_ROOT = Path('/srv/deploy-backups/environment-triad/staging-switches')
FAILURE_LIMIT = 3


def _sha256_tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def _probe(version: str) -> dict:
    result = health('staging', version)
    request = urllib.request.Request('http://127.0.0.1:48126/api/query/%E5%85%A8%E9%93%BE%E6%9D%A1?page=1&page_size=1')
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status >= 500:
        raise SnapshotError('staging_critical_endpoint_failed')
    if status not in {200, 401, 403}:
        raise SnapshotError('staging_critical_endpoint_contract_changed')
    result['critical_endpoint_status'] = status
    return result


def _wait_stable(version: str) -> dict:
    failures = 0
    last = None
    for _ in range(45):
        try:
            last = _probe(version)
            failures = 0
            # Require three consecutive successful health rounds too.
            for _ in range(FAILURE_LIMIT - 1):
                time.sleep(2)
                last = _probe(version)
            return last
        except (OSError, ValueError, urllib.error.URLError, SnapshotError):
            failures += 1
            if failures >= FAILURE_LIMIT:
                break
            time.sleep(2)
    raise SnapshotError('staging_health_failed_three_times')


def _candidate_is_verified(snapshot_id: str) -> dict:
    result = candidate_execute('verify', snapshot_id)
    if (result.get('ready_for_application_switch') is not True
            or result.get('sensitive_value_matches') != 0
            or result.get('reference_integrity') is not True
            or result.get('row_counts_verified') is not True
            or result.get('idempotent_import') is not True
            or result.get('pending_gates') != []):
        raise SnapshotError('staging_candidate_verification_incomplete')
    return result


def switch(snapshot_id: str) -> dict:
    import fcntl
    candidate = database_names(snapshot_id)
    snapshot_root = ROOT / snapshot_id
    safe_directory(snapshot_root)
    if not (snapshot_root / 'snapshot.json').is_file():
        raise SnapshotError('snapshot_artifact_missing')
    verification = _candidate_is_verified(snapshot_id)
    root = root_for('staging')
    evidence = EVIDENCE_ROOT / snapshot_id
    if evidence.exists() or evidence.is_symlink():
        raise SnapshotError('staging_switch_evidence_exists')
    safe_directory(EVIDENCE_ROOT, create=True)
    evidence.mkdir(mode=0o700)
    with (root / '.deployment.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest, compose, env_text = read_configuration(root)
        values = parse_environment(env_text)
        if values.get('APP_ENVIRONMENT') != 'staging' or values.get('MYSQL_HOST') != 'environment-mysql':
            raise SnapshotError('staging_application_identity_mismatch')
        previous = evidence / 'previous'
        previous.mkdir(mode=0o700)
        for name in ('backend.env', 'manifest.json', 'compose.json', 'init.sql'):
            shutil.copyfile(root / name, previous / name)
        private_json(evidence / 'previous-config-sha256.json', _sha256_tree(previous))
        backup_databases('staging', root, manifest, evidence)
        private_json(evidence / 'verification.json', verification)
        started = time.monotonic()
        changed = False
        try:
            for key, domain in zip(KEYS, DOMAINS):
                values['MYSQL_' + key + '_DB'] = candidate[domain]
            new_env = '\n'.join(key + '=' + value for key, value in values.items()) + '\n'
            new_manifest = dict(manifest)
            new_manifest['databases'] = candidate
            new_manifest['staging_snapshot_id'] = snapshot_id
            prepared = evidence / 'candidate'
            prepared.mkdir(mode=0o700)
            (prepared / 'backend.env').write_text(new_env, encoding='utf-8', newline='\n')
            new_manifest['hashes'] = {
                'compose.json': file_hash(root / 'compose.json'),
                'backend.env': file_hash(prepared / 'backend.env'),
                'init.sql': file_hash(root / 'init.sql'),
            }
            private_json(prepared / 'manifest.json', new_manifest)
            changed = True
            replace_file(prepared / 'backend.env', root / 'backend.env')
            replace_file(prepared / 'manifest.json', root / 'manifest.json')
            command(['docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d', '--no-deps', 'backend'])
            stable = _wait_stable(values['APP_VERSION'])
            live, _, live_env = read_configuration(root)
            if live.get('staging_snapshot_id') != snapshot_id or parse_environment(live_env).get('MYSQL_ONLINE_DATA_DB') != candidate['OnlineData']:
                raise SnapshotError('staging_snapshot_switch_identity_mismatch')
            report = {'environment': 'staging', 'snapshot_id': snapshot_id, 'switched': True,
                      'rollback_required': False, 'health_failure_limit': FAILURE_LIMIT,
                      'health': stable, 'elapsed_seconds': round(time.monotonic() - started, 3),
                      'production_modified': False}
            private_json(evidence / 'result.json', report)
            return report
        except Exception:
            rollback = 'not_required'
            if changed:
                try:
                    replace_file(previous / 'backend.env', root / 'backend.env')
                    replace_file(previous / 'manifest.json', root / 'manifest.json')
                    command(['docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d', '--no-deps', 'backend'])
                    previous_values = parse_environment(env_text)
                    _wait_stable(previous_values.get('APP_VERSION') or manifest.get('version'))
                    rollback = 'previous_staging_application_restored'
                except Exception:
                    rollback = 'staging_application_restore_failed'
            private_json(evidence / 'failure.json', {'environment': 'staging', 'snapshot_id': snapshot_id,
                'reason': 'staging_snapshot_switch_failed', 'rollback': rollback,
                'production_modified': False})
            raise SnapshotError('staging_snapshot_switch_failed') from None


if __name__ == '__main__':
    raise SystemExit('use the fixed Staging data gateway')
