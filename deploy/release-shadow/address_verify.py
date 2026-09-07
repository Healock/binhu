"""Synthetic address annotation SQL/transaction acceptance in a guarded shadow runner."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request
from fastapi import HTTPException
from verify import guard, require


async def main():
    evidence = {'checks': [], 'status': 'failed'}
    initialized = False
    try:
        evidence['run_id'] = await guard()
        from database import init_db, close_db, db_manager, ensure_online_editor_schema
        from services.local_source import create_local_source_row
        from services.online_source import rebuild_projection_rows
        from services.address_match_feedback import feedback_hmac
        from routers import mobile_tasks as routes
        await init_db()
        initialized = True
        async with db_manager.get_pool('online_data').acquire() as conn:
            async with conn.cursor() as cur:
                # Empty, new isolated schema only: reconstruct the previous version's shape.
                for column in ('manual_unmatched_reason', 'manual_unmatched_address_hmac', 'manual_unmatched_by', 'manual_unmatched_at'):
                    await cur.execute(f'ALTER TABLE _online_task_address_matches DROP COLUMN {column}')
                for column in ('source_id', 'source_revision'):
                    await cur.execute(f'ALTER TABLE _online_task_address_unmatched_events DROP COLUMN {column}')
                await ensure_online_editor_schema(cur)
                await ensure_online_editor_schema(cur)
                await conn.commit()
                evidence['checks'].append('old_schema_additive_upgrade_and_repeat_initialization')
                values = {'姓名': '虚构验收任务', '身份证号': 'SYNTHETIC-ADDRESS-RELEASE', '电话号码': 'SYNTHETIC-PHONE', '地址': '虚构验收路1号', '现住址': '', '社区': '虚构验收社区', '核查结果': ''}
                source = await create_local_source_row(cur, '全链条', values, source_kind='local_table', source_ref='address-release:' + evidence['run_id'])
                await rebuild_projection_rows(cur, '全链条', [source['row_key']])
                positive = feedback_hmac(values['地址'], values['社区'])
                await cur.execute("INSERT INTO _online_address_match_feedback (address_hmac,community_id,confirmed_entry_id,status) VALUES (%s,999001,999001,'active')", (positive,))
                await conn.commit()
            user = {'id': 999001, 'username': 'synthetic-release', 'role': 'super_admin', 'permissions': ['online.raw.edit', 'online.task.manage']}
            request = Request({'type': 'http', 'method': 'POST', 'path': '/synthetic', 'headers': [], 'client': ('127.0.0.1', 1)})
            data = routes.AddressMatchManualUnmatched(source_id=source['id'], expected_revision=1, expected_row_hash=source['row_hash'], reason_code='community_registry_missing')
            await routes.mark_mobile_task_address_manual_unmatched('全链条', source['row_key'], data, request, user, conn)
            async with conn.cursor() as cur:
                await cur.execute('SELECT match_status,confirmed_entry_id,manual_unmatched_reason,LENGTH(manual_unmatched_address_hmac) FROM _online_task_address_matches WHERE row_key=%s', (source['row_key'],))
                require(await cur.fetchone() == ('manual_unmatched', None, 'community_registry_missing', 64), 'human conclusion was not persisted')
                await cur.execute('SELECT source_id,source_revision FROM _online_task_address_unmatched_events WHERE row_key=%s', (source['row_key'],))
                require(await cur.fetchall() == ((source['id'], 2),), 'history version mismatch')
                await cur.execute('SELECT status,confirmed_entry_id FROM _online_address_match_feedback WHERE address_hmac=%s', (positive,))
                require(await cur.fetchone() == ('conflict', None), 'positive feedback was not disabled')
                await rebuild_projection_rows(cur, '全链条', [source['row_key']])
                await conn.commit()
            evidence['checks'].append('real_route_annotation_revision_history_feedback_and_rebuild')
            try:
                await routes.mark_mobile_task_address_manual_unmatched('全链条', source['row_key'], data, request, user, conn)
            except HTTPException as error:
                require(error.status_code == 409, 'wrong stale revision response')
            else:
                raise AssertionError('stale revision was accepted')
            evidence['checks'].append('stale_revision_409')
            async def fail_rebuild(*args, **kwargs):
                raise RuntimeError('synthetic rollback injection')
            fresh = data.model_copy(update={'expected_revision': 2})
            with patch.object(routes, 'rebuild_projection_rows', fail_rebuild):
                try:
                    await routes.mark_mobile_task_address_manual_unmatched('全链条', source['row_key'], fresh, request, user, conn)
                except RuntimeError as error:
                    require(str(error) == 'synthetic rollback injection', 'unexpected failure')
                else:
                    raise AssertionError('failure injection missing')
            async with conn.cursor() as cur:
                await cur.execute('SELECT revision FROM _online_source_rows WHERE id=%s', (source['id'],))
                require((await cur.fetchone())[0] == 2, 'failed transaction changed revision')
                await cur.execute('SELECT COUNT(*) FROM _online_task_address_unmatched_events WHERE row_key=%s', (source['row_key'],))
                require((await cur.fetchone())[0] == 1, 'failed transaction left history')
                await cur.execute('SELECT COUNT(*) FROM _online_summary_updates WHERE task_id=%s AND revision=3', (source['local_task_id'],))
                require((await cur.fetchone())[0] == 0, 'failed transaction left summary input')
            evidence['checks'].append('injected_failure_rolls_back_history_revision_summary')
        evidence['status'] = 'passed'
    except BaseException as error:
        evidence['error_type'] = type(error).__name__
        raise
    finally:
        Path('/artifacts/address-verify.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        if initialized:
            await close_db()
    print(json.dumps(evidence))


if __name__ == '__main__':
    asyncio.run(main())
