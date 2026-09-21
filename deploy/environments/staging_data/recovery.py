"""Approved, in-memory reconstruction of missing model-three sources only.

Never writes to the source connection. Each recovered row needs a unique current
business row and active local-source ledger with identical values/key/hash.
"""
import hashlib
import json
from .codec import SnapshotError

PARSER = '疑似未注销模型三'
MAX_RECOVERED = 261
LEDGER_FIELDS = ('parser_type', 'local_task_id', 'business_key', 'status',
    'archived_at', 'source_kind', 'source_ref', 'values_json', 'content_hash', 'revision')


def _missing_rows(parser, business, sources):
    if parser.parser_type != PARSER:
        raise SnapshotError('source_recovery_parser_not_approved')
    if (len({r['id'] for r in business}) != len(business)
            or len({r['_row_key'] for r in business}) != len(business)):
        raise SnapshotError('duplicate_current_business_key')
    current = {(r['physical_row'], r['row_key']) for r in sources if r['parser_type'] == PARSER}
    return sorted(
        [r for r in business if (r['id'], r['_row_key']) not in current],
        key=lambda row: (int(row['id']), str(row['_row_key'])),
    )


def _safe_date(value):
    text = str(value or '').strip()
    if not text:
        return None
    # Keep the evidence aggregate bounded and date-only. Invalid values are
    # omitted from the summary; they never become exported Staging data here.
    try:
        from datetime import datetime
        if len(text) == 5 and text[2] == '-':
            parsed = datetime.strptime('2000-' + text, '%Y-%m-%d')
            return text
        parsed = datetime.fromisoformat(text.replace('T', ' ', 1).replace('Z', '+00:00'))
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        for fmt in ('%Y/%m/%d', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
        return None


def recovery_scope(parser, business, sources, *, community_digest=None, limit=MAX_RECOVERED):
    """Select a deterministic bounded subset and return aggregate-only overflow evidence.

    The returned evidence contains no business identifiers or source text. A
    caller supplies a snapshot-scoped HMAC community digest; the fallback is
    only for pure unit tests and is not used by the exporter.
    """
    missing = _missing_rows(parser, business, sources)
    if limit < 1:
        raise SnapshotError('source_recovery_limit_invalid')
    selected = missing[:limit]
    excluded = missing[limit:]
    grouped = {}
    for row in excluded:
        values = row
        community = str(values.get(parser.COMMUNITY_COLUMN) or '').strip()
        if community_digest is None:
            key = hashlib.sha256(community.encode('utf-8')).hexdigest()[:16]
        else:
            key = str(community_digest(community))
        entry = grouped.setdefault(key, {'community_key': key, 'count': 0,
                                         'date_min': None, 'date_max': None})
        entry['count'] += 1
        date_text = _safe_date(values.get('截止时间'))
        if date_text and (entry['date_min'] is None or date_text < entry['date_min']):
            entry['date_min'] = date_text
        if date_text and (entry['date_max'] is None or date_text > entry['date_max']):
            entry['date_max'] = date_text
    dates = [_safe_date(row.get('截止时间')) for row in excluded]
    dates = [item for item in dates if item]
    return selected, {
        'parser_type': PARSER,
        'maximum_recovered_sources': limit,
        'candidate_missing_count': len(missing),
        'recovered_count': len(selected),
        'excluded_count': len(excluded),
        'excluded_date_min': min(dates) if dates else None,
        'excluded_date_max': max(dates) if dates else None,
        'excluded_by_community': [grouped[key] for key in sorted(grouped)],
    }


def _conflict_summary(rows, *, community_digest=None):
    """Return bounded aggregate diagnostics for ledger conflicts only."""
    by_type = {}
    by_community = {}
    dates = []
    revisions = []
    for item in rows:
        kind = item['type']
        by_type[kind] = by_type.get(kind, 0) + 1
        key = item.get('community') or ''
        if community_digest is not None:
            key = str(community_digest(key))
        else:
            key = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
        entry = by_community.setdefault(key, {'community_key': key, 'count': 0})
        entry['count'] += 1
        if item.get('date'):
            dates.append(item['date'])
        if isinstance(item.get('revision'), int):
            revisions.append(item['revision'])
    return {
        'conflict_count': len(rows),
        'conflict_by_type': by_type,
        'conflict_by_community': [by_community[key] for key in sorted(by_community)],
        'conflict_date_min': min(dates) if dates else None,
        'conflict_date_max': max(dates) if dates else None,
        'conflict_revision_min': min(revisions) if revisions else None,
        'conflict_revision_max': max(revisions) if revisions else None,
    }


def reconstruct(parser, business, sources, ledgers, reserved_ids, *,
                community_digest=None, limit=MAX_RECOVERED,
                exclude_approved_conflicts=False, return_diagnostics=False):
    selected, _scope = recovery_scope(
        parser, business, sources, community_digest=community_digest, limit=limit
    )
    next_id = max(reserved_ids, default=0) + 1
    recovered = []
    conflicts = []
    for row in selected:
        # Include partially matching active ledgers: conflicting key or ID is
        # ambiguous, even if a second ledger would otherwise match exactly.
        candidates = [r for r in ledgers if r['parser_type'] == PARSER
            and (r['local_task_id'] == row['id'] or r['business_key'] == row['_row_key'])]
        matches = [r for r in candidates if r['status'] == 'active']
        if len(matches) != 1:
            values = {column: str(row[column] or '') for column in parser.COLUMNS}
            if candidates and not matches:
                raise SnapshotError('source_recovery_ledger_mismatch')
            conflicts.append({'type': 'missing_active_ledger' if not matches else 'multiple_active_ledgers',
                'community': values.get(parser.COMMUNITY_COLUMN),
                'date': _safe_date(values.get('截止时间')),
                'revision': None})
            if not exclude_approved_conflicts:
                raise SnapshotError('source_recovery_ledger_not_unique')
            continue
        ledger = matches[0]
        values = {column: str(row[column] or '') for column in parser.COLUMNS}
        serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        try:
            ledger_values = json.loads(ledger['values_json']) if isinstance(ledger['values_json'], str) else ledger['values_json']
        except (ValueError, TypeError):
            raise SnapshotError('source_recovery_ledger_invalid_json') from None
        if ledger['local_task_id'] != row['id'] or ledger['business_key'] != row['_row_key']:
            conflicts.append({'type': 'task_id_business_key_conflict',
                'community': values.get(parser.COMMUNITY_COLUMN),
                'date': _safe_date(values.get('截止时间')),
                'revision': ledger.get('revision') if isinstance(ledger.get('revision'), int) else None})
            if not exclude_approved_conflicts:
                raise SnapshotError('source_recovery_ledger_not_unique')
            continue
        if (parser.make_row_key(values) != row['_row_key']
                or ledger_values != values or ledger['content_hash'] != digest
                or ledger['archived_at'] is not None
                or ledger['source_kind'] not in {'local_table', 'local_dispatch', 'one_time_continuation_import'}
                or not isinstance(ledger['source_ref'], str) or not ledger['source_ref']
                or type(ledger['revision']) is not int or not 1 <= ledger['revision'] <= 2**63-1):
            raise SnapshotError('source_recovery_ledger_mismatch')
        recovered.append({'id': next_id, 'parser_type': PARSER, 'physical_row': row['id'],
            'revision': ledger['revision'], 'row_key': row['_row_key'], 'row_hash': digest,
            'values_json': serialized, 'source_kind': ledger['source_kind']})
        next_id += 1
    if conflicts and not exclude_approved_conflicts:
        raise SnapshotError('source_recovery_ledger_not_unique', diagnostics=_conflict_summary(
            conflicts, community_digest=community_digest))
    diagnostics = _conflict_summary(conflicts, community_digest=community_digest) if conflicts else {
        'conflict_count': 0, 'conflict_by_type': {}, 'conflict_by_community': [],
        'conflict_date_min': None, 'conflict_date_max': None,
        'conflict_revision_min': None, 'conflict_revision_max': None}
    return (recovered, diagnostics) if return_diagnostics else recovered
