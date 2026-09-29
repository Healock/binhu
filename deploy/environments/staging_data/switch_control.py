"""Atomically switch only the Staging backend to a verified snapshot."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import time
import urllib.error
import urllib.request

from .apply_control import execute as candidate_execute
from .codec import SnapshotError
from .control import ROOT, private_json, safe_directory
from .target import DOMAINS, KEYS, database_names
from ..artifact import file_hash
from ..runtime import SPEC, root_for
from ..update import backup_databases, command, parse_environment, read_configuration, replace_file


EVIDENCE_ROOT = Path('/srv/deploy-backups/environment-triad/staging-switches')
FAILURE_LIMIT = 3
STARTUP_ATTEMPTS = 45
FAILURE_REASON_RE = re.compile(r'[a-z0-9_]{1,100}')


def _failure_code(exc: BaseException) -> str:
    reason = getattr(exc, 'reason', '')
    if not reason and exc.args and isinstance(exc.args[0], str):
        reason = exc.args[0]
    return reason if isinstance(reason, str) and FAILURE_REASON_RE.fullmatch(reason) else 'staging_snapshot_switch_failed'


def _sha256_tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def _new_evidence_path(snapshot_id: str) -> Path:
    """Allocate a non-overwriting evidence directory for this switch attempt."""
    parent = EVIDENCE_ROOT / snapshot_id
    if not parent.exists():
        parent.mkdir(mode=0o700)
        return parent
    safe_directory(parent)
    for attempt in range(100):
        candidate = parent / f'retry-{time.time_ns()}-{attempt}'
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
    raise SnapshotError('staging_switch_evidence_allocation_failed')


def _probe_record(*, stage='', bootstrap_status_code=None, environment_match=None,
                  version_match=None, api_entry_match=None, health_status_code=None,
                  query_status_code=None, error_code='') -> dict:
    """Build the allowlisted probe envelope; never include response data."""
    return {
        'stage': stage,
        'bootstrap_status_code': bootstrap_status_code,
        'bootstrap_environment_match': environment_match,
        'bootstrap_version_match': version_match,
        'bootstrap_api_entry_match': api_entry_match,
        'health_status_code': health_status_code,
        'critical_query_status_code': query_status_code,
        'error_code': error_code,
    }


def _http_status_and_json(url: str) -> tuple[int | None, dict | None]:
    """Read only status and the small identity payload; discard all other data."""
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = response.status
            if status != 200:
                return status, None
            try:
                payload = json.load(response)
            except (ValueError, TypeError):
                payload = None
            return status, payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def _probe(version: str) -> dict:
    port = SPEC['staging'][2]
    expected_api_entry = '/' + SPEC['staging'][1] + '/api'
    bootstrap_url = f'http://127.0.0.1:{port}/api/app/bootstrap'
    status, payload = _http_status_and_json(bootstrap_url)
    if status != 200:
        record = _probe_record(stage='bootstrap', bootstrap_status_code=status,
                               error_code='staging_bootstrap_http_error')
        raise SnapshotError(record['error_code'], diagnostics=record)
    if payload is None:
        record = _probe_record(stage='bootstrap', bootstrap_status_code=status,
                               error_code='staging_bootstrap_payload_invalid')
        raise SnapshotError(record['error_code'], diagnostics=record)
    environment_match = payload.get('environment') == 'staging'
    version_match = payload.get('server_version') == version
    api_entry_match = payload.get('api_entry') == expected_api_entry
    if not (environment_match and version_match and api_entry_match):
        record = _probe_record(stage='bootstrap', bootstrap_status_code=status,
                               environment_match=environment_match,
                               version_match=version_match,
                               api_entry_match=api_entry_match,
                               error_code='staging_bootstrap_identity_mismatch')
        raise SnapshotError(record['error_code'], diagnostics=record)

    health_url = f'http://127.0.0.1:{port}/api/health'
    health_status, _ = _http_status_and_json(health_url)
    if health_status != 200:
        record = _probe_record(stage='health', bootstrap_status_code=status,
                               environment_match=True, version_match=True,
                               api_entry_match=True, health_status_code=health_status,
                               error_code='staging_health_http_error')
        raise SnapshotError(record['error_code'], diagnostics=record)

    query_url = f'http://127.0.0.1:{port}/api/query/%E5%85%A8%E9%93%BE%E6%9D%A1?page=1&page_size=1'
    query_status, _ = _http_status_and_json(query_url)
    if query_status is not None and query_status >= 500:
        error_code = 'staging_critical_endpoint_failed'
    elif query_status not in {200, 401, 403}:
        error_code = 'staging_critical_endpoint_contract_changed'
    else:
        return {
            'environment': 'staging', 'version': version, 'health': True,
            'critical_endpoint_status': query_status,
            **_probe_record(stage='', bootstrap_status_code=status,
                            environment_match=True, version_match=True,
                            api_entry_match=True, health_status_code=health_status,
                            query_status_code=query_status),
        }
    record = _probe_record(stage='query', bootstrap_status_code=status,
                           environment_match=True, version_match=True,
                           api_entry_match=True, health_status_code=health_status,
                           query_status_code=query_status, error_code=error_code)
    raise SnapshotError(error_code, diagnostics=record)


def _wait_stable(version: str) -> dict:
    failures = 0
    successes = 0
    ready_once = False
    last = None
    probe_attempts = []
    for _ in range(STARTUP_ATTEMPTS):
        try:
            last = _probe(version)
            ready_once = True
            failures = 0
            successes += 1
            if successes >= FAILURE_LIMIT:
                return last
        except (OSError, ValueError, urllib.error.URLError, SnapshotError) as exc:
            diagnostic = getattr(exc, 'diagnostics', None)
            if isinstance(diagnostic, dict):
                probe_attempts.append(diagnostic)
                last = diagnostic
            successes = 0
            failures += 1
            # Allow the new process a bounded startup window. Once it has
            # served a healthy probe, three consecutive failures are a real
            # post-start health regression and trigger the rollback gate.
            if ready_once and failures >= FAILURE_LIMIT:
                break
        if _ < STARTUP_ATTEMPTS - 1:
            time.sleep(2)
    diagnostics = {}
    if isinstance(last, dict):
        diagnostics['probe'] = last
    if probe_attempts:
        diagnostics['probe_attempts'] = probe_attempts
    raise SnapshotError('staging_health_failed_three_times', diagnostics=diagnostics)


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
    safe_directory(EVIDENCE_ROOT, create=True)
    evidence = _new_evidence_path(snapshot_id)
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
        phase = 'candidate_preparation'
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
            phase = 'configuration_switch'
            replace_file(prepared / 'backend.env', root / 'backend.env')
            replace_file(prepared / 'manifest.json', root / 'manifest.json')
            phase = 'backend_restart'
            command(['docker', 'compose', '-f', str(root / 'compose.json'), 'up', '-d', '--no-deps', 'backend'])
            phase = 'health_probe'
            stable = _wait_stable(values['APP_VERSION'])
            phase = 'live_identity_check'
            live, _, live_env = read_configuration(root)
            if live.get('staging_snapshot_id') != snapshot_id or parse_environment(live_env).get('MYSQL_ONLINE_DATA_DB') != candidate['OnlineData']:
                raise SnapshotError('staging_snapshot_switch_identity_mismatch')
            report = {'environment': 'staging', 'snapshot_id': snapshot_id, 'switched': True,
                      'rollback_required': False, 'health_failure_limit': FAILURE_LIMIT,
                      'health': stable, 'elapsed_seconds': round(time.monotonic() - started, 3),
                      'production_modified': False}
            private_json(evidence / 'result.json', report)
            return report
        except Exception as exc:
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
            failure = {'environment': 'staging', 'snapshot_id': snapshot_id,
                'reason': _failure_code(exc), 'phase': phase, 'exception_type': type(exc).__name__,
                'rollback': rollback, 'production_modified': False}
            diagnostics = getattr(exc, 'diagnostics', {})
            if isinstance(diagnostics, dict):
                for key in ('probe', 'probe_attempts'):
                    value = diagnostics.get(key)
                    if value:
                        failure[key] = value
            private_json(evidence / 'failure.json', failure)
            raise SnapshotError('staging_snapshot_switch_failed') from None


if __name__ == '__main__':
    raise SystemExit('use the fixed Staging data gateway')
