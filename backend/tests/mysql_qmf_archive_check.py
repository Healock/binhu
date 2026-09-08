"""Opt-in isolated MySQL check; never run against the production database."""
import ast
import asyncio
import json
import os
from pathlib import Path

import aiomysql


async def main():
    assert os.environ.get('APP_ENVIRONMENT') == 'shadow'
    assert os.environ.get('MYSQL_HOST') == 'mysql'
    assert os.environ.get('LOAD_TEST_RUN_ID') == 'qmf-archive-20260908b'
    pool = await aiomysql.create_pool(host='mysql', user='root', password=os.environ['MYSQL_ROOT_PASSWORD'], autocommit=False, charset='utf8mb4')
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SHOW DATABASES')
            assert {r[0] for r in await cur.fetchall()} <= {'mysql', 'information_schema', 'performance_schema', 'sys'}
            await cur.execute('CREATE DATABASE OnlineData')
            await cur.execute('CREATE DATABASE OnlineDataArchive')
            await cur.execute('USE OnlineData')
            columns = ['截止时间', '核查人', '姓名', '身份证号', '联系方式', '地址', '下发社区', '核查结果', '备注']
            body = ','.join(f'`{c}` VARCHAR(200)' for c in columns)
            await cur.execute(f'CREATE TABLE t_suspect_unrevoked (_row_key VARCHAR(32) PRIMARY KEY,{body}) ENGINE=InnoDB')
            await cur.execute(f'CREATE TABLE OnlineDataArchive.t_suspect_unrevoked_archive (id INT AUTO_INCREMENT PRIMARY KEY,_row_key VARCHAR(32),{body},_archive_reason VARCHAR(100)) ENGINE=InnoDB')
            await cur.execute('CREATE TABLE _online_source_rows (id INT PRIMARY KEY,parser_type VARCHAR(50),row_key VARCHAR(32),archived_at DATETIME) ENGINE=InnoDB')
            await cur.execute('CREATE TABLE _online_source_projection (parser_type VARCHAR(50),row_key VARCHAR(32),task_state VARCHAR(30)) ENGINE=InnoDB')
            await cur.execute('CREATE TABLE _qmf_status_snapshots (parser_type VARCHAR(50),row_key VARCHAR(32),feedback_state VARCHAR(30),archived_at DATETIME,archive_due_at DATETIME) ENGINE=InnoDB')
            await cur.execute('CREATE TABLE _local_source_records (id INT PRIMARY KEY,parser_type VARCHAR(50),business_key VARCHAR(32),status VARCHAR(20),revision INT,archived_at DATETIME,updated_at DATETIME) ENGINE=InnoDB')
        await conn.commit()
    pool.close()
    await pool.wait_closed()
    pool = await aiomysql.create_pool(host='mysql', user='root', password=os.environ['MYSQL_ROOT_PASSWORD'], db='OnlineData', autocommit=False, charset='utf8mb4')
    source = Path('/checks/qmf_status_scan.py').read_text()
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'archive_due_qmf_tasks')
    # Execute the exact changed function; graph reconciliation is unchanged and
    # tested separately by the project suite. No external status client runs.
    async def graph(cur, parser):
        pass
    scope = {'_pool': lambda: pool, 'MODEL_THREE_PARSER': '疑似未注销模型三', 'reconcile_projection_task_graph': graph}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual archive function>', 'exec'), scope)
    archive = scope['archive_due_qmf_tasks']
    parser = scope['MODEL_THREE_PARSER']

    async def seed(key, offset):
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('INSERT INTO t_suspect_unrevoked (_row_key,`姓名`) VALUES (%s,%s)', (key, '虚构验收'))
                await cur.execute('INSERT INTO _online_source_rows VALUES (%s,%s,%s,NULL)', (offset,parser,key))
                await cur.execute('INSERT INTO _online_source_projection VALUES (%s,%s,\'completed\')', (parser,key))
                await cur.execute('INSERT INTO _qmf_status_snapshots VALUES (%s,%s,\'completed_match\',NULL,UTC_TIMESTAMP()-INTERVAL 1 DAY)', (parser,key))
                await cur.execute('INSERT INTO _local_source_records VALUES (%s,%s,%s,\'active\',3,NULL,UTC_TIMESTAMP())', (offset,parser,key))
            await conn.commit()

    async def rows(sql, args=()):
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql,args)
                result = await cur.fetchall()
            await conn.commit()
            return result

    await seed('fixture-a',1)
    await rows('INSERT INTO _local_source_records VALUES (2,%s,\'fixture-a\',\'superseded\',9,NULL,UTC_TIMESTAMP()),(3,\'全链条\',\'fixture-a\',\'active\',4,NULL,UTC_TIMESTAMP())',(parser,))
    await rows('INSERT INTO _online_source_rows VALUES (99,%s,\'fixture-a\',UTC_TIMESTAMP())',(parser,))
    assert await archive() == 1
    assert await rows('SELECT status,revision FROM _local_source_records ORDER BY id') == (('archived',4),('superseded',9),('active',4))
    assert await rows('SELECT id FROM _online_source_rows') == ((99,),)
    assert await rows('SELECT COUNT(*) FROM t_suspect_unrevoked') == ((0,),)
    assert await rows('SELECT COUNT(*) FROM OnlineDataArchive.t_suspect_unrevoked_archive') == ((1,),)
    assert await archive() == 0
    assert await rows('SELECT revision FROM _local_source_records WHERE id=1') == ((4,),)

    await seed('fixture-b',10)
    for trigger, table, action in [('fail_ledger','_local_source_records','UPDATE'),('fail_archive','OnlineDataArchive.t_suspect_unrevoked_archive','INSERT')]:
        trigger_name = 'OnlineDataArchive.'+trigger if '.' in table else trigger
        await rows(f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table} FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='synthetic_failure'")
        try:
            await archive()
            raise AssertionError('Failure must abort the transaction')
        except aiomysql.MySQLError as error:
            assert 'synthetic_failure' in str(error)
        assert await rows('SELECT COUNT(*) FROM t_suspect_unrevoked') == ((1,),)
        assert await rows('SELECT status,revision FROM _local_source_records WHERE id=10') == (('active',3),)
        assert await rows('SELECT COUNT(*) FROM OnlineDataArchive.t_suspect_unrevoked_archive') == ((1,),)
        await rows(f'DROP TRIGGER {trigger_name}')
    assert await archive() == 1
    assert await archive() == 0
    pool.close()
    await pool.wait_closed()
    print(json.dumps({'passed':True,'checks':['archive_and_local_revision','preserve_terminal_ledger','cross_parser_isolation','preserve_archived_sources','repeat_noop','ledger_failure_rollback','archive_failure_rollback','retry_after_failure']},ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
