"""Read-only source reconciliation summaries; keys never leave the process."""
from collections import Counter, defaultdict


def summarize(business, sources, ledgers, archive_keys):
    business_keys = {(r['id'], r['_row_key']) for r in business}
    source_keys = {(r['physical_row'], r['row_key']) for r in sources}
    ledger_states = defaultdict(set)
    for row in ledgers:
        ledger_states[(row['local_task_id'], row['business_key'])].add(row['status'])
    missing = business_keys - source_keys
    return {
        'business_count': len(business), 'source_count': len(sources),
        'business_only_count': len(missing), 'source_only_count': len(source_keys - business_keys),
        'duplicate_business_key_count': sum(n - 1 for n in Counter(r['_row_key'] for r in business).values()),
        'duplicate_source_key_count': sum(n - 1 for n in Counter(r['row_key'] for r in sources).values()),
        'business_only_active_ledger_count': sum('active' in ledger_states[key] for key in missing),
        'business_only_archived_ledger_count': sum(ledger_states[key] == {'archived'} for key in missing),
        'business_only_no_ledger_count': sum(not ledger_states[key] for key in missing),
        'business_only_archive_key_count': sum(key[1] in archive_keys for key in missing),
    }
