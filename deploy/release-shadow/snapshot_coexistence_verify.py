"""Synthetic release acceptance for daily baseline and periodic reconciliation."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from verify import guard
from summary_verify import create_fixture, mutate_fixture, read_ledger, require, seed_organization, pool_execute


async def main():
    result = {'status': 'failed', 'step': 'guard', 'checks': []}
    initialized = False
    try:
        result['run_id'] = await guard()
        from database import init_db, close_db, db_manager
        from config import settings
        from services.business_time import current_business_date
        from services.online_summary_updates import process_online_summary_updates_once
        from services.local_report_scheduler import refresh_local_daily_reports_once
        await init_db()
        initialized = True
        token = uuid4().hex[:12]
        today = current_business_date('Asia/Shanghai')
        community, inspector, _ = await seed_organization(token)
        task = await create_fixture(token, label='carryover', state='', revision=1,
                                    business_date=today-timedelta(days=3),
                                    inspector=inspector, community=community)
        # Keep this task unchanged today. Only its first-seen timestamp is
        # synthetic fixture setup; no summary trigger for today's date.
        online = db_manager.get_pool('online_data')
        await pool_execute(online, 'UPDATE t_suspect_return SET _first_seen_at=%s,'
                           '_last_updated_at=%s WHERE id=%s',
                           (today-timedelta(days=3), today-timedelta(days=3), task['task_id']))
        result['step'] = 'daily_baseline'
        # Call the same baseline the enabled production worker uses without
        # starting a scheduler or changing the shadow safety configuration.
        snapshot = await refresh_local_daily_reports_once()
        require(snapshot['status'] == 'success', 'daily baseline failed')
        ledger = await read_ledger(task, today)
        require(ledger is not None and ledger[0] == 'unchecked' and ledger[1] == 0,
                'unchanged carryover missing or credited')
        result['checks'].append('real_baseline_contains_unchanged_carryover')
        result['step'] = 'incremental_update'
        await mutate_fixture(token, task, state='无需登记', revision=2,
                             business_date=today, inspector=inspector, community=community)
        await process_online_summary_updates_once(limit=50)
        before = await read_ledger(task, today)
        require(before[0] == 'completed' and before[1] == 1 and before[2] == 2,
                'incremental update failed after baseline')
        result['step'] = 'periodic_snapshot_after_increment'
        snapshot = await refresh_local_daily_reports_once()
        require(snapshot['status'] == 'success', 'periodic snapshot failed')
        after = await read_ledger(task, today)
        require(after == before, 'periodic snapshot overwrote fenced incremental ledger')
        result['checks'].append('periodic_snapshot_preserves_incremental_revision_and_workload')
        result['status'] = 'passed'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        result['mysql_errno'] = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        raise
    finally:
        if initialized:
            await close_db()
        Path('/artifacts/snapshot-coexistence.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))


if __name__ == '__main__':
    asyncio.run(main())
