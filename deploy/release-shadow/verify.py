"""Real MySQL release checks; only the isolated release runner may execute this."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiomysql


DOMAIN_KEYS = ('ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM',
               'VISIT', 'DISPATCH', 'REGISTRY', 'WORKFLOW')


class ShadowGuardError(RuntimeError):
    pass


def expected_databases(run_id: str) -> dict[str, str]:
    prefix = 'ReleaseShadow_' + run_id.replace('-', '_')
    return {key: f'{prefix}_{index}' for index, key in enumerate(DOMAIN_KEYS)}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ShadowGuardError(message)


async def guard() -> str:
    run_id = os.environ.get('LOAD_TEST_RUN_ID', '')
    require(run_id != '', 'missing shadow run id')
    expected = expected_databases(run_id)
    require(os.environ.get('APP_ENVIRONMENT') == 'shadow', 'shadow environment identity mismatch')
    require(os.environ.get('MYSQL_HOST') == 'mysql', 'unexpected MySQL host')
    require(os.environ.get('MYSQL_USER') == 'release_shadow', 'unexpected MySQL user')
    for key in DOMAIN_KEYS:
        require(os.environ.get(f'MYSQL_{key}_DB') == expected[key], f'database identity mismatch: {key}')
    require(os.environ.get('TXDOCS_ENABLED', '').lower() == 'false', 'external document source must be disabled')
    for key in ('REALTIME_EVENTS_ENABLED', 'LOCAL_REPORT_SCHEDULER_ENABLED',
                'QMF_SOURCE_ACQUISITION_ENABLED', 'QMF_REGISTRATION_ENABLED',
                'VENUE_CLOUD_SYNC_ENABLED', 'VENUE_CLOUD_PULL_ENABLED',
                'CERTIFICATE_SOURCE_DAILY_ENABLED'):
        require(os.environ.get(key, '').lower() == 'false', f'shadow safety switch must be disabled: {key}')
    conn = await aiomysql.connect(host='mysql', user=os.environ['MYSQL_USER'],
                                  password=os.environ['MYSQL_PASSWORD'],
                                  db=expected['ONLINE_DATA'], connect_timeout=5)
    try:
        async with conn.cursor() as cur:
            await cur.execute('SELECT run_id FROM _release_shadow_identity')
            require(await cur.fetchall() == ((run_id,),), 'shadow identity table mismatch')
            await cur.execute('SELECT DATABASE()')
            require((await cur.fetchone())[0] == expected['ONLINE_DATA'], 'connected database mismatch')
    finally:
        conn.close()
    return run_id


async def main():
    results = {'run_id': os.environ.get('LOAD_TEST_RUN_ID', ''), 'checks': []}
    db_initialized = False
    try:
        run_id = await guard()
        from database import init_db, close_db, db_manager
        from services.online_summary_updates import ensure_online_summary_update_schema, enqueue_online_summary_update
        from services.business_time import current_business_date
        await init_db()
        db_initialized = True
        results['checks'].append('fresh_eight_database_initialization')
        await guard()
        results['checks'].append('shadow_identity_and_safety_switches')
        pool = db_manager.get_pool('online_data')
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await ensure_online_summary_update_schema(cur)
                await cur.execute('SHOW COLUMNS FROM _online_summary_updates')
                columns = {row[0] for row in await cur.fetchall()}
                require({'task_id', 'revision', 'business_date', 'status'} <= columns,
                        'summary update schema is incomplete')
                await conn.begin()
                kwargs = dict(task_id=999001, parser_type='疑似返苏', row_key='f' * 32,
                              revision=1, business_date=current_business_date('Asia/Shanghai'), operation_id='release-synthetic-rollback')
                await enqueue_online_summary_update(cur, **kwargs)
                await enqueue_online_summary_update(cur, **kwargs)
                await cur.execute('SELECT COUNT(*) FROM _online_summary_updates WHERE task_id=%s', (999001,))
                require((await cur.fetchone())[0] == 1, 'summary trigger is not idempotent')
                await conn.rollback()
                await cur.execute('SELECT COUNT(*) FROM _online_summary_updates WHERE task_id=%s', (999001,))
                require((await cur.fetchone())[0] == 0, 'business rollback left a summary trigger')
        results['checks'].append('real_driver_summary_enqueue_dedup_and_business_rollback')
        results['status'] = 'passed'
    except BaseException as exc:
        results['status'] = 'failed'
        results['error_type'] = type(exc).__name__
        results['error_message'] = str(exc)[:300]
        raise
    finally:
        Path('/artifacts/mysql-bootstrap.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        if db_initialized:
            await close_db()
    print(json.dumps(results))


if __name__ == '__main__':
    asyncio.run(main())
