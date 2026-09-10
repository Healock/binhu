"""Build an empty candidate schema from the existing isolated Staging schema.

No source connection is accepted. All DDL targets newly named Staging databases;
failed candidates remain as evidence, while the application keeps its old DBs.
"""
from .codec import SnapshotError
from .target import target_settings, qualified


async def measure(conn, settings, snapshot_id):
    current, candidate = target_settings(settings, snapshot_id)
    inventory = {}
    async with conn.cursor() as cur:
        for domain, database in current.items():
            await cur.execute('SELECT id,environment FROM `' + database + '`._environment_identity')
            if list(await cur.fetchall()) != [(1, 'staging')]:
                raise SnapshotError('current_staging_marker_mismatch')
            await cur.execute('SELECT table_name,engine,table_type FROM information_schema.tables WHERE table_schema=%s', (database,))
            tables = await cur.fetchall()
            if not tables or any(engine != 'InnoDB' or kind != 'BASE TABLE' for _,engine,kind in tables):
                raise SnapshotError('unsupported_staging_schema_objects')
            for table, _, _ in tables:
                qualified(candidate, domain+'.'+table)
            inventory[domain] = sorted(table for table,_,_ in tables)
            await cur.execute('SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=%s', (database,))
            if (await cur.fetchone())[0]:
                raise SnapshotError('staging_schema_triggers_require_review')
            await cur.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s', (candidate[domain],))
            if (await cur.fetchone())[0]:
                raise SnapshotError('candidate_database_already_exists')
    return {'snapshot_id': snapshot_id, 'current': current, 'candidate': candidate,
            'tables': inventory, 'ready_for_application_switch': False}


async def create(conn, settings, snapshot_id):
    plan = await measure(conn, settings, snapshot_id)
    # Caller must hold the per-environment advisory/OS lock for this entire
    # operation. CREATE DATABASE deliberately has no IF NOT EXISTS shortcut.
    async with conn.cursor() as cur:
        for domain, database in plan['candidate'].items():
            await cur.execute('CREATE DATABASE `' + database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
            for table in plan['tables'][domain]:
                await cur.execute('CREATE TABLE ' + qualified(plan['candidate'], domain+'.'+table)
                    + ' LIKE `' + plan['current'][domain] + '`.`' + table + '`')
            await cur.execute('INSERT INTO `' + database + '`._environment_identity (id,environment) VALUES (1,%s)', ('staging',))
            await conn.commit()
    return plan
