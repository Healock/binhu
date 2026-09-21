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
            elif sys.argv[1:] == ['measure']:
                result = measure()
            elif len(sys.argv) == 3 and action == 'export':
                result = export(snapshot_id)
            elif len(sys.argv) == 3 and action in {'create','import','verify'}:
                result = candidate(action, snapshot_id)
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
            if action in {'export','create','import','verify','switch'}:
                _alert(action, snapshot_id, reason)
            raise SystemExit('Staging data gateway refused; inspect private evidence') from None
        except Exception:
            reason = 'staging_data_operation_failed'
            _audit(action or 'invalid', 'failed', snapshot_id=snapshot_id, reason=reason)
            if action in {'export','create','import','verify','switch'}:
                _alert(action, snapshot_id, reason)
            raise SystemExit('Staging data gateway refused; inspect private evidence') from None


if __name__ == '__main__':
    main()
