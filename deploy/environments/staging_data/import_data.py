"""Transactionally populate an empty, identity-checked Staging candidate."""
import copy
import json
import re
import secrets
from .codec import SnapshotError
from .digests import identity_digest, address_digest, annotation_digest
from .target import target_settings, qualified


def materialize(snapshot, key):
    from services.parsers import get_parser
    from services.task_workflow import TASK_WORKFLOWS
    import bcrypt
    report=snapshot['report']
    if (report.get('sensitive_value_matches') != 0 or report.get('reference_integrity') is not True
            or set(report.get('pending_gates',[])) != {'registration_hmac_rebuild','candidate_database_import','target_verification'}):
        raise SnapshotError('snapshot_preparation_gates_incomplete')
    tables=copy.deepcopy(snapshot['tables'])
    sources={(row['parser_type'],row['row_key']):json.loads(row['values_json'])
        for row in tables['OnlineData._online_source_rows']}
    properties={row['id']:row for row in tables['RegistryData.registry_properties']}
    states={(row['parser_type'],row['row_key']):row for row in snapshot['registration_digest_states']}
    for row in tables['OnlineData._task_registration_links']:
        task_key=(row['parser_type'],row['row_key'])
        state=states.pop(task_key)
        if state['identity']=='current':
            row['identity_hmac']=identity_digest(sources[task_key],TASK_WORKFLOWS[row['parser_type']].identity_fields,key)
        elif state['identity'] not in {'empty','stale'}:
            raise SnapshotError('invalid_registration_digest_state')
        if state['address']=='current':
            row['last_address_hmac']=address_digest(properties[row['property_id']]['natural_address'],key)
        elif state['address'] not in {'empty','stale'}:
            raise SnapshotError('invalid_registration_digest_state')
    if states:
        raise SnapshotError('extra_registration_digest_state')
    states={(row['parser_type'],row['row_key']):row for row in snapshot['annotation_digest_states']}
    for row in tables['OnlineData._online_task_address_matches']:
        task_key=(row['parser_type'],row['row_key'])
        state=states.pop(task_key)['state']
        if state=='current':
            parser=get_parser(row['parser_type'])
            values=sources[task_key]
            row['manual_unmatched_address_hmac']=annotation_digest(values,TASK_WORKFLOWS[row['parser_type']],values[parser.COMMUNITY_COLUMN],key)
        elif state=='stale':
            # Preserve an invalid annotation rather than silently making it
            # valid for the newly generated address.
            row['manual_unmatched_address_hmac']=secrets.token_hex(32)
        elif state!='empty':
            raise SnapshotError('invalid_annotation_digest_state')
    if states:
        raise SnapshotError('extra_annotation_digest_state')
    # These are history/role fixtures. No production password is copied and
    # no generated plaintext credential is retained or exposed.
    # bcrypt rejects NUL bytes; use URL-safe entropy for the intentionally
    # inaccessible placeholder password instead of raw random bytes.
    inaccessible_hash=bcrypt.hashpw(secrets.token_urlsafe(32).encode(),bcrypt.gensalt()).decode()
    for row in tables['PlatformData._users']:
        row['password_hash']=inaccessible_hash
    return tables


def column_sql(columns):
    if any(not re.fullmatch(r'[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*', name) for name in columns):
        raise SnapshotError('invalid_import_column')
    return ','.join('`'+name+'`' for name in columns)


async def import_rows(conn, settings, snapshot, *, before_commit=None):
    snapshot_id=snapshot['report']['snapshot_id']
    _,candidate=target_settings(settings,snapshot_id)
    tables=materialize(snapshot,settings.registry_hmac_key)
    try:
        async with conn.cursor() as cur:
            for database in candidate.values():
                await cur.execute('SELECT id,environment FROM `'+database+'`._environment_identity')
                if list(await cur.fetchall()) != [(1,'staging')]:
                    raise SnapshotError('candidate_marker_mismatch')
            # Validate every table before the first INSERT, never use REPLACE,
            # UPSERT or TRUNCATE on a failed/existing candidate.
            for logical,rows in tables.items():
                table=qualified(candidate,logical)
                await cur.execute('SELECT COUNT(*) FROM '+table)
                if (await cur.fetchone())[0]:
                    raise SnapshotError('candidate_table_not_empty')
                if rows:
                    columns=tuple(rows[0])
                    column_sql(columns)
                    if any(set(row)!=set(columns) for row in rows):
                        raise SnapshotError('inconsistent_import_columns')
            await conn.begin()
            for logical,rows in tables.items():
                if not rows:continue
                columns=tuple(rows[0])
                sql='INSERT INTO '+qualified(candidate,logical)+' ('+column_sql(columns)+') VALUES ('+','.join(['%s']*len(columns))+')'
                for offset in range(0,len(rows),250):
                    await cur.executemany(sql,[tuple(row[name] for name in columns) for row in rows[offset:offset+250]])
            for logical,rows in tables.items():
                await cur.execute('SELECT COUNT(*) FROM '+qualified(candidate,logical))
                if (await cur.fetchone())[0]!=len(rows):
                    raise SnapshotError('candidate_import_count_mismatch')
            if before_commit is not None:
                await before_commit(cur, candidate)
            await conn.commit()
        return {'snapshot_id':snapshot_id,'import_counts':{name:len(rows) for name,rows in tables.items()},
                'ready_for_application_switch':False,'pending_gates':['target_verification','projection_rebuild','observer_initialization']}
    except BaseException:
        await conn.rollback()
        raise
