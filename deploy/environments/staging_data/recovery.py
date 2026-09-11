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


def reconstruct(parser, business, sources, ledgers, reserved_ids):
    if parser.parser_type != PARSER:
        raise SnapshotError('source_recovery_parser_not_approved')
    if (len({r['id'] for r in business}) != len(business)
            or len({r['_row_key'] for r in business}) != len(business)):
        raise SnapshotError('duplicate_current_business_key')
    current = {(r['physical_row'], r['row_key']) for r in sources if r['parser_type'] == PARSER}
    missing = [r for r in business if (r['id'], r['_row_key']) not in current]
    if len(missing) > MAX_RECOVERED:
        raise SnapshotError('source_recovery_scope_exceeded')
    next_id = max(reserved_ids, default=0) + 1
    recovered = []
    for row in missing:
        # Include partially matching active ledgers: conflicting key or ID is
        # ambiguous, even if a second ledger would otherwise match exactly.
        matches = [r for r in ledgers if r['parser_type'] == PARSER and r['status'] == 'active'
            and (r['local_task_id'] == row['id'] or r['business_key'] == row['_row_key'])]
        if len(matches) != 1:
            raise SnapshotError('source_recovery_ledger_not_unique')
        ledger = matches[0]
        values = {column: str(row[column] or '') for column in parser.COLUMNS}
        serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        try:
            ledger_values = json.loads(ledger['values_json']) if isinstance(ledger['values_json'], str) else ledger['values_json']
        except (ValueError, TypeError):
            raise SnapshotError('source_recovery_ledger_invalid_json') from None
        if (ledger['local_task_id'] != row['id'] or ledger['business_key'] != row['_row_key']
                or parser.make_row_key(values) != row['_row_key']
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
    return recovered
