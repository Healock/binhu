"""Fixed Staging sanitized snapshot gateway.

Only the reviewed Production read-only exporter and Staging candidate database
operations are reachable.  Callers cannot supply paths, database names, Docker
targets, shell fragments, or environment names.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time

from .artifact import write_json
from .staging_data.apply_control import execute as candidate_execute
from .staging_data.codec import SnapshotError
from .staging_data.control import ROOT, execute as source_execute, safe_directory
from .staging_data import switch_control
from .staging_data.switch_control import switch as switch_snapshot
from .staging_data.target import database_names


BASE = Path('/var/lib/binhu-staging-data-gateway')
AUDIT_ROOT = Path('/var/log/binhu-staging-gateways')
SNAPSHOT_RE = re.compile(r'staging-[0-9a-f]{16}')
POLICY = 'approved-sanitized-scope-v1'
_REASON = re.compile(r'[a-z0-9_]{1,100}')


def refuse(message='fixed Staging data command required'):
    raise SnapshotError(message)


def _safe_failure_reason(exc: BaseException) -> str:
    """Keep fixed internal reason codes while never serializing exception text."""
    reason = getattr(exc, 'reason', '')
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else 'staging_data_operation_failed'


def _safe_root(path: Path, *, create=False):
    if not path.is_absolute() or path != path.resolve() or any(item.is_symlink() for item in (path, *path.parents)):
        refuse('staging_data_path_identity_invalid')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        refuse('staging_data_path_permissions_invalid')
    return path


def _audit(action, outcome, *, snapshot_id='', reason=''):
    _safe_root(AUDIT_ROOT, create=True)
    if snapshot_id and not SNAPSHOT_RE.fullmatch(snapshot_id):
        snapshot_id = ''
    if reason and not re.fullmatch(r'[a-z0-9_]{1,100}', reason):
        reason = 'staging_data_operation_failed'
    entry = {'timestamp': int(time.time()), 'gateway': 'staging-data', 'action': action,
             'outcome': outcome, 'snapshot_id': snapshot_id, 'reason': reason}
    path = AUDIT_ROOT / 'data-audit.jsonl'
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, sort_keys=True) + '\n')
        stream.flush(); os.fsync(stream.fileno())


def _alert(action, snapshot_id, reason):
    _safe_root(AUDIT_ROOT, create=True)
    suffix = snapshot_id if SNAPSHOT_RE.fullmatch(snapshot_id or '') else 'unassigned'
    path = AUDIT_ROOT / f'data-alert-{suffix}-{int(time.time())}.json'
    write_json(path, {'timestamp': int(time.time()), 'gateway': 'staging-data',
        'action': action, 'snapshot_id': snapshot_id if suffix != 'unassigned' else '',
        'severity': 'error', 'reason': reason})
    path.chmod(0o600)


def _snapshot(snapshot_id):
    database_names(snapshot_id)
    path = ROOT / snapshot_id
    safe_directory(path)
    if path.parent != ROOT or path.is_symlink():
        refuse('snapshot_path_identity_invalid')
    return path


def _record(path: Path, action: str, value=None):
    target = path / ('gateway-' + action + '.json')
    if target.is_file() and not target.is_symlink():
        return json.loads(target.read_text(encoding='utf-8'))
    if target.exists() or target.is_symlink():
        refuse('snapshot_gateway_record_invalid')
    if value is None:
        return None
    write_json(target, value)
    target.chmod(0o600)
    return value


def measure():
    result = source_execute('measure', exclude_orphan_property_links=True,
                            recover_model_three_sources=True,
                            staging_sample_mode=True, staging_sample_limit=150)
    _audit('measure', 'passed', snapshot_id=result['snapshot_id'])
    return result


def export(policy):
    if policy != POLICY:
        refuse('fixed_sanitization_policy_required')
    result = source_execute('export', exclude_orphan_property_links=True,
                            recover_model_three_sources=True,
                            staging_sample_mode=True, staging_sample_limit=150)
    path = _snapshot(result['snapshot_id'])
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    if report.get('sensitive_value_matches') != 0 or report.get('reference_integrity') is not True:
        refuse('snapshot_sanitization_gate_failed')
    _record(path, 'export', {'snapshot_id': result['snapshot_id'], 'policy': POLICY,
        'sensitive_value_matches': 0, 'reference_integrity': True})
    _audit('export', 'passed', snapshot_id=result['snapshot_id'])
    return result


def candidate(action, snapshot_id):
    path = _snapshot(snapshot_id)
    if _record(path, 'export') is None:
        refuse('snapshot_export_gate_missing')
    sequence = {'create': None, 'import': 'create', 'verify': 'import'}
    required = sequence[action]
    if required and _record(path, required) is None:
        refuse('snapshot_previous_gate_missing')
    previous = _record(path, action)
    if previous is not None:
        return previous
    result = candidate_execute(action, snapshot_id)
    if action == 'import':
        result = {**result, 'idempotent_replay': True}
    _record(path, action, result)
    _audit(action, 'passed', snapshot_id=snapshot_id)
    return result


def reverify(snapshot_id):
    """Run the read-only target verification again without changing cached records."""
    path = _snapshot(snapshot_id)
    if _record(path, 'export') is None:
        refuse('snapshot_export_gate_missing')
    result = candidate_execute('verify', snapshot_id)
    evidence = path / f'gateway-reverify-{int(time.time())}.json'
    if evidence.exists() or evidence.is_symlink():
        refuse('snapshot_reverify_evidence_exists')
    write_json(evidence, result)
    evidence.chmod(0o600)
    _audit('reverify', 'passed', snapshot_id=snapshot_id)
    return result


def switch(snapshot_id):
    path = _snapshot(snapshot_id)
    verified = _record(path, 'verify')
    if not verified or verified.get('ready_for_application_switch') is not True:
        refuse('snapshot_verification_gate_missing')
    previous = _record(path, 'switch')
    if previous is not None:
        return previous
    result = switch_snapshot(snapshot_id)
    _record(path, 'switch', result)
    _audit('switch', 'passed', snapshot_id=snapshot_id)
    return result


def failure_diagnostics(snapshot_id):
    """Return bounded, fixed-code failure evidence for one Staging snapshot."""
    _snapshot(snapshot_id)
    root = ROOT / snapshot_id
    failures = []
    attempt_re = re.compile(r'candidate-(create|import|verify)-[0-9a-f]{12}')
    domains = set(database_names(snapshot_id))
    for attempt in root.iterdir():
        match = attempt_re.fullmatch(attempt.name)
        if not match or attempt.is_symlink() or not attempt.is_dir():
            continue
        evidence = attempt / 'failure.json'
        if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 65536:
            continue
        try:
            value = json.loads(evidence.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict) or set(value) - {'reason', 'diagnostics', 'exit_code'}:
            continue
        reason = value.get('reason')
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            reason = 'staging_candidate_operation_failed'
        diagnostics = value.get('diagnostics')
        safe = {}
        if isinstance(diagnostics, dict) and len(diagnostics) <= 16:
            domain = diagnostics.get('domain')
            if isinstance(domain, str) and domain in domains:
                safe['domain'] = domain
            table_name = diagnostics.get('table_name')
            if isinstance(table_name, str) and re.fullmatch(r'[A-Za-z0-9_$.-]{1,128}', table_name):
                safe['table_name'] = table_name
            for key in ('missing_tables', 'extra_tables', 'missing_columns', 'extra_columns',
                        'missing_indexes', 'extra_indexes', 'missing_constraints', 'extra_constraints'):
                items = diagnostics.get(key)
                if (isinstance(items, list) and len(items) <= 256
                        and all(isinstance(item, str)
                                and re.fullmatch(r'[A-Za-z0-9_$.-]{1,128}', item) for item in items)):
                    safe[key] = items
            for key in ('expected_table_count', 'actual_table_count',
                        'expected_row_count', 'actual_row_count'):
                item = diagnostics.get(key)
                if type(item) is int and 0 <= item <= 10000:
                    safe[key] = item
            if diagnostics.get('schema_signature_mismatch') is True:
                safe['schema_signature_mismatch'] = True
            for key in ('expected_signature_sha256', 'actual_signature_sha256'):
                item = diagnostics.get(key)
                if isinstance(item, str) and re.fullmatch(r'[0-9a-f]{64}', item):
                    safe[key] = item
        entry = {'action': match.group(1), 'reason': reason}
        if match.group(1) == 'create' and reason == 'staging_candidate_job_timeout':
            phase = 'candidate_creation' if (attempt / 'code-hashes.json').is_file() else 'pre_create_schema_measure'
            entry['phase'] = phase
        if safe:
            entry['diagnostics'] = safe
        exit_code = value.get('exit_code')
        if type(exit_code) is int and -255 <= exit_code <= 255:
            entry['exit_code'] = exit_code
        failures.append((evidence.stat().st_mtime_ns, entry))
    failures.sort(key=lambda item: item[0], reverse=True)
    result = {'snapshot_id': snapshot_id, 'failures': [entry for _, entry in failures[:8]]}
    switch_info = _switch_failure_diagnostics(snapshot_id)
    if switch_info:
        result['switch'] = switch_info
    return result


def _switch_failure_diagnostics(snapshot_id):
    result = {}
    evidence_root = switch_control.EVIDENCE_ROOT / snapshot_id
    evidence_dirs = []
    if evidence_root.is_dir() and not evidence_root.is_symlink():
        evidence_dirs.append(evidence_root)
        evidence_dirs.extend(sorted(
            path for path in evidence_root.iterdir()
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r'retry-[0-9]+-[0-9]+', path.name)
        ))
    evidence_dir = evidence_dirs[-1] if evidence_dirs else evidence_root
    if evidence_dirs:
        names = {
            'database_backup_complete': 'backup.json',
            'verification_evidence_saved': 'verification.json',
            'candidate_configuration_prepared': 'candidate',
            'previous_configuration_saved': 'previous-config-sha256.json',
        }
        milestones = {}
        for field, name in names.items():
            path = evidence_dir / name
            milestones[field] = path.is_dir() if name == 'candidate' else path.is_file() and not path.is_symlink()
        result['milestones'] = milestones
    if (evidence_dirs
            and (evidence_dir / 'failure.json').is_file()
            and not (evidence_dir / 'failure.json').is_symlink()):
        try:
            safe_directory(evidence_dir)
            if (evidence_dir / 'failure.json').stat().st_size <= 16384:
                value = json.loads((evidence_dir / 'failure.json').read_text(encoding='utf-8'))
                if (isinstance(value, dict) and value.get('environment') == 'staging'
                        and value.get('snapshot_id') == snapshot_id
                        and value.get('production_modified') is False):
                    reason = value.get('reason')
                    rollback = value.get('rollback')
                    if isinstance(reason, str) and _REASON.fullmatch(reason):
                        result['reason'] = reason
                    if rollback in {'not_required', 'previous_staging_application_restored',
                                    'staging_application_restore_failed'}:
                        result['rollback'] = rollback
                    result['production_modified'] = False
                    phase = value.get('phase')
                    if phase in {'database_backup', 'verification_evidence', 'candidate_preparation',
                                 'configuration_switch', 'backend_restart', 'health_probe',
                                 'live_identity_check'}:
                        result['phase'] = phase
                    exception_type = value.get('exception_type')
                    if exception_type in {'SnapshotError', 'OSError', 'ValueError', 'RuntimeError',
                                          'TimeoutExpired', 'URLError', 'HTTPError'}:
                        result['exception_type'] = exception_type
                    probe = value.get('probe')
                    if isinstance(probe, dict):
                        safe_probe = {}
                        stage = probe.get('stage')
                        if stage in {'', 'bootstrap', 'health', 'query'}:
                            safe_probe['stage'] = stage
                        for key in ('bootstrap_status_code', 'health_status_code',
                                    'critical_query_status_code'):
                            item = probe.get(key)
                            if item is None or (type(item) is int and 100 <= item <= 599):
                                safe_probe[key] = item
                        for key in ('bootstrap_environment_match', 'bootstrap_version_match',
                                    'bootstrap_api_entry_match'):
                            item = probe.get(key)
                            if type(item) is bool:
                                safe_probe[key] = item
                        error_code = probe.get('error_code')
                        if isinstance(error_code, str) and _REASON.fullmatch(error_code):
                            safe_probe['error_code'] = error_code
                        if safe_probe:
                            result['probe'] = safe_probe
        except (OSError, ValueError, SnapshotError):
            pass
    try:
        _safe_root(AUDIT_ROOT)
        matches = sorted(AUDIT_ROOT.glob(f'data-alert-{snapshot_id}-*.json'))[-8:]
        for path in reversed(matches):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                continue
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            reason = value.get('reason') if isinstance(value, dict) else None
            if (isinstance(value, dict) and value.get('action') == 'switch'
                    and value.get('snapshot_id') == snapshot_id
                    and isinstance(reason, str) and _REASON.fullmatch(reason)):
                result['gateway_reason'] = reason
                break
    except (OSError, SnapshotError):
        pass
    return result


def status():
    snapshots = []
    if ROOT.is_dir() and not ROOT.is_symlink():
        for path in sorted(ROOT.iterdir()):
            if SNAPSHOT_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                completed = [name for name in ('export','create','import','verify','switch')
                             if (path / ('gateway-' + name + '.json')).is_file()]
                snapshots.append({'snapshot_id': path.name, 'completed': completed})
    return {'gateway': 'staging-data', 'source_access': 'production-read-only-sanitized',
            'target_environment': 'staging', 'snapshots': snapshots[-10:]}


def main():
    import fcntl
    if os.name != 'posix' or os.geteuid() != 0:
        raise SystemExit('root execution required')
    os.umask(0o077)
    _safe_root(BASE, create=True)
    with (BASE / 'gateway.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        action = sys.argv[1] if len(sys.argv) > 1 else ''
        snapshot_id = sys.argv[2] if len(sys.argv) > 2 else ''
        try:
            if sys.argv[1:] == ['status']:
                result = status()
            elif len(sys.argv) == 3 and action == 'diagnose' and SNAPSHOT_RE.fullmatch(snapshot_id):
                result = failure_diagnostics(snapshot_id)
                _audit('diagnose', 'read', snapshot_id=snapshot_id)
            elif sys.argv[1:] == ['measure']:
                result = measure()
            elif len(sys.argv) == 3 and action == 'export':
                result = export(snapshot_id)
            elif len(sys.argv) == 3 and action in {'create','import','verify'}:
                result = candidate(action, snapshot_id)
            elif len(sys.argv) == 3 and action == 'reverify':
                result = reverify(snapshot_id)
            elif len(sys.argv) == 3 and action == 'switch':
                result = switch(snapshot_id)
            else:
                refuse()
            if action == 'status':
                _audit('status', 'passed')
            print(json.dumps(result, sort_keys=True))
        except SnapshotError as exc:
            reason = _safe_failure_reason(exc)
            _audit(action or 'invalid', 'failed', snapshot_id=snapshot_id, reason=reason)
            if action in {'export','create','import','verify','reverify','switch'}:
                _alert(action, snapshot_id, reason)
            raise SystemExit('Staging data gateway refused; inspect private evidence') from None
        except Exception:
            reason = 'staging_data_operation_failed'
            _audit(action or 'invalid', 'failed', snapshot_id=snapshot_id, reason=reason)
            if action in {'export','create','import','verify','reverify','switch'}:
                _alert(action, snapshot_id, reason)
            raise SystemExit('Staging data gateway refused; inspect private evidence') from None


if __name__ == '__main__':
    main()
