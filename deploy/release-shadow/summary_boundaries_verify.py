"""Guarded synthetic checks for archive, sparse history and duplicate memberships."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from verify import guard
from summary_verify import (
    create_fixture, mutate_fixture, pool_query, pool_execute,
    process_once_and_require, read_ledger, require, seed_organization,
)


async def main():
    result = {'status': 'failed', 'checks': [], 'step': 'guard'}
    initialized = False
    try:
        result['run_id'] = await guard()
        from database import init_db, close_db, db_manager
        from services.business_time import current_business_date
        from services.fullchain_archive_jobs import _run_local_archive_export
        from services.report_builders.summary import get_summary
        await init_db()
        initialized = True
        await guard()
        token = uuid4().hex[:12]
        today = current_business_date('Asia/Shanghai')
        community, inspector, member_id = await seed_organization(token)
        online = db_manager.get_pool('online_data')
        platform = db_manager.get_pool('platform')
        result['step'] = 'sparse_history'
        task = await create_fixture(token, label='sparse', state='无需登记', revision=1,
                                    business_date=today-timedelta(days=3),
                                    inspector=inspector, community=community)
        await process_once_and_require()
        await mutate_fixture(token, task, state='无需登记', revision=2, business_date=today,
                             inspector=inspector, community=community)
        await process_once_and_require()
        require((await read_ledger(task, today))[1] == 0, 'sparse history credited twice')
        result['checks'].append('sparse_history_no_duplicate_workload')

        result['step'] = 'multiple_departments'
        other_community, _, _ = await seed_organization(token+'b')
        department = await pool_query(platform, 'SELECT id FROM _departments WHERE name=%s',
                                      (other_community,))
        await pool_execute(platform, 'INSERT INTO _grid_member_department_links '
                           '(member_id,department_id,sort_order) VALUES (%s,%s,1)',
                           (member_id, department[0]))
        await mutate_fixture(token, task, state='无需登记', revision=3, business_date=today,
                             inspector=inspector, community=community)
        await process_once_and_require()
        public = await get_summary(today.isoformat())
        rows = [r for r in public['community']['data'] if r['社区'] == community]
        require(len(rows) == 1 and rows[0]['数据总数'] == 1, 'membership doubled task count')
        result['checks'].append('multiple_departments_count_once')

        result['step'] = 'local_archive'
        conn = await online.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute('INSERT INTO _fullchain_archive_exports '
                                  '(export_no,parser_type,categories_json,total_count) '
                                  "VALUES (%s,'疑似返苏','{}',1)", ('synthetic-'+token,))
                export_id = int(cur.lastrowid)
                await cur.execute('INSERT INTO _fullchain_archive_export_items '
                                  '(export_id,parser_type,row_key,source_id,spreadsheet_id,sheet_id,'
                                  'physical_row,expected_revision,expected_row_hash,source_values_json,category) '
                                  'SELECT %s,parser_type,row_key,id,spreadsheet_id,sheet_id,physical_row,'
                                  "revision,row_hash,values_json,'无需登记' FROM _online_source_rows "
                                  'WHERE parser_type=%s AND row_key=%s AND archived_at IS NULL',
                                  (export_id, '疑似返苏', task['row_key']))
                require(cur.rowcount == 1, 'archive source ambiguous')
            await conn.commit()
            await _run_local_archive_export(conn, export_id)
            await conn.commit()
            async with conn.cursor() as cur:
                await cur.execute('SELECT status FROM _fullchain_archive_exports WHERE id=%s', (export_id,))
                require((await cur.fetchone())[0] == 'completed', 'local archive failed')
        finally:
            online.release(conn)
        await process_once_and_require()
        ledger = await read_ledger(task, today)
        require(ledger[2] == 4, 'archive revision did not reach ledger')
        archived = await pool_query(online, 'SELECT revision,archived_at FROM _online_source_rows '
                                    'WHERE parser_type=%s AND row_key=%s', ('疑似返苏', task['row_key']))
        require(archived[0] == 4 and archived[1] is not None, 'archive source fence missing')
        projection = await pool_query(db_manager.get_pool('daily_report'),
                                      'SELECT online_present,included FROM _daily_task_ledger '
                                      'WHERE report_date=%s AND parser_type=%s AND row_key=%s',
                                      (today, '疑似返苏', task['row_key']))
        require(projection == (0, 1), 'archive lost credited completed task')
        result['checks'].append('real_local_archive_new_revision_preserves_completed_work')
        result['status'] = 'passed'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        result['mysql_errno'] = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        raise
    finally:
        if initialized:
            await close_db()
        Path('/artifacts/summary-boundaries.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


if __name__ == '__main__':
    asyncio.run(main())
