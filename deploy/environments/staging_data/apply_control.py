"""Run candidate schema/import jobs in a bounded, isolated Staging container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from .codec import SnapshotError
from .control import ROOT, safe_directory, private_json, preflight
from .target import database_names
from ..database_identity import validate_resources

MODULES=('codec','digests','target','candidate','import_data')


def command(args, *, stdin=None, timeout=30):
    result=subprocess.run(args,input=stdin,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        # Do not emit argv/stdout/stderr: they can include connection details.
        raise SnapshotError('staging_candidate_command_failed',
                            diagnostics={'exit_code':result.returncode})
    return result.stdout


def resources():
    ids=command(['docker','ps','-aq']).split()
    containers=json.loads(command(['docker','inspect',*ids]))
    named={c['Name'].lstrip('/'):c for c in containers}
    backend=named['binhu-staging-backend-1']
    mysql=named['binhu-staging-environment-mysql-1']
    network=json.loads(command(['docker','network','inspect','binhu-staging_internal']))[0]
    validate_resources('staging',backend,mysql,network,containers)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',backend['Image']):
        raise SnapshotError('immutable_staging_image_required')
    return backend,mysql


def grant_sql(snapshot_id):
    # MySQL database-level grants treat underscore as a wildcard unless escaped.
    return '\n'.join("GRANT ALL PRIVILEGES ON `"+name.replace('_',r'\_')+"`.* TO 'environment_app'@'%';"
        for name in database_names(snapshot_id).values())


def program(snapshot_id, action):
    modules={name:(Path(__file__).parent/(name+'.py')).read_text(encoding='utf-8') for name in MODULES}
    code="import sys,types,json,asyncio\npackage=types.ModuleType('snapshot_tool');package.__path__=[];sys.modules['snapshot_tool']=package\n"
    code+='sources='+repr(modules)+'\n'
    code+="for name,source in sources.items():\n    module=types.ModuleType('snapshot_tool.'+name);module.__package__='snapshot_tool';sys.modules[module.__name__]=module;exec(compile(source,'<snapshot_tool.'+name+'>','exec'),module.__dict__)\n"
    code+='snapshot_id='+repr(snapshot_id)+'\naction='+repr(action)+'\n'
    # Parse data as JSON, not as a Python literal. Large snapshots otherwise
    # expand into millions of compiler AST nodes before the job can start,
    # exhausting the bounded container even though the data fits in memory.
    code+="snapshot=json.load(sys.stdin) if action in {'measure','import','verify'} else None\n"
    code+='''
from config import settings
import aiomysql
from snapshot_tool.codec import SnapshotError
from snapshot_tool.target import target_settings
from snapshot_tool.candidate import measure,create
from snapshot_tool.import_data import import_rows,materialize
async def schema_signature(cur,database,table):
    await cur.execute('SELECT column_name,column_type,is_nullable,column_default,extra,generation_expression FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',(database,table))
    columns=list(await cur.fetchall())
    await cur.execute('SELECT index_name,non_unique,seq_in_index,column_name,sub_part,index_type FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s ORDER BY index_name,seq_in_index',(database,table))
    indexes=list(await cur.fetchall())
    await cur.execute('SELECT constraint_name,constraint_type FROM information_schema.table_constraints WHERE table_schema=%s AND table_name=%s ORDER BY constraint_name',(database,table))
    constraints=list(await cur.fetchall())
    return {'columns':[list(row) for row in columns],'indexes':[list(row) for row in indexes],
        'constraints':[list(row) for row in constraints]}
async def verify_current_schema(conn,snapshot,current):
    production_contract=snapshot.get('schema_contract')
    if not isinstance(production_contract,dict):raise SnapshotError('source_schema_contract_missing')
    async with conn.cursor() as cur:
        for domain,database in current.items():
            await cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=%s ORDER BY table_name',(database,'BASE TABLE'))
            tables=[row[0] for row in await cur.fetchall()]
            expected_tables=set(production_contract.get(domain,{}))|{'_environment_identity'}
            actual_tables=set(tables)
            if actual_tables!=expected_tables:
                raise SnapshotError('production_staging_schema_table_mismatch', diagnostics={
                    'domain':domain,'missing_tables':sorted(expected_tables-actual_tables),
                    'extra_tables':sorted(actual_tables-expected_tables),
                    'expected_table_count':len(expected_tables),'actual_table_count':len(actual_tables)})
            for table in tables:
                if table!='_environment_identity' and await schema_signature(cur,database,table)!=production_contract[domain][table]:
                    raise SnapshotError('production_staging_schema_mismatch', diagnostics={
                        'domain':domain,'table_name':table,'schema_signature_mismatch':True})
    return True
async def verify_target(conn,settings,snapshot,current,candidate):
    expected=materialize(snapshot,settings.registry_hmac_key)
    expected_counts={name:len(rows) for name,rows in expected.items()}
    production_contract=snapshot.get('schema_contract')
    if not isinstance(production_contract,dict):raise SnapshotError('source_schema_contract_missing')
    schema_objects=0
    async with conn.cursor() as cur:
        for domain in current:
            for database in (current[domain],candidate[domain]):
                await cur.execute('SELECT id,environment FROM `'+database+'`._environment_identity')
                if list(await cur.fetchall())!=[(1,'staging')]:raise SnapshotError('target_environment_marker_mismatch')
            await cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=%s ORDER BY table_name',(current[domain],'BASE TABLE'))
            current_tables=[row[0] for row in await cur.fetchall()]
            await cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type=%s ORDER BY table_name',(candidate[domain],'BASE TABLE'))
            candidate_tables=[row[0] for row in await cur.fetchall()]
            if current_tables!=candidate_tables:raise SnapshotError('target_schema_table_mismatch')
            expected_tables=set(production_contract.get(domain,{}))|{'_environment_identity'}
            actual_tables=set(candidate_tables)
            if actual_tables!=expected_tables:
                raise SnapshotError('production_staging_schema_table_mismatch', diagnostics={
                    'domain':domain,'missing_tables':sorted(expected_tables-actual_tables),
                    'extra_tables':sorted(actual_tables-expected_tables),
                    'expected_table_count':len(expected_tables),'actual_table_count':len(actual_tables)})
            for table in current_tables:
                signatures=[]
                for database in (current[domain],candidate[domain]):
                    signatures.append(await schema_signature(cur,database,table))
                if signatures[0]!=signatures[1]:raise SnapshotError('target_schema_signature_mismatch')
                expected_signature=production_contract.get(domain,{}).get(table)
                actual_signature=signatures[1]
                if table!='_environment_identity' and expected_signature!=actual_signature:
                    raise SnapshotError('production_staging_schema_mismatch', diagnostics={
                        'domain':domain,'table_name':table,'schema_signature_mismatch':True})
                logical=domain+'.'+table
                expected_count=expected_counts.get(logical,0)
                if logical=='PlatformData._users':expected_count+=1
                if table=='_environment_identity':expected_count=1
                await cur.execute('SELECT COUNT(*) FROM `'+candidate[domain]+'`.`'+table+'`')
                if (await cur.fetchone())[0]!=expected_count:raise SnapshotError('target_row_count_mismatch')
                schema_objects+=1
        await cur.execute('SELECT COUNT(*) FROM `'+candidate['PlatformData']+'`._users WHERE username=%s',('observer@staging',))
        if (await cur.fetchone())[0]!=1:raise SnapshotError('observer_initialization_failed')
    report=snapshot['report']
    if report.get('sensitive_value_matches')!=0 or report.get('reference_integrity') is not True:
        raise SnapshotError('target_sanitization_evidence_invalid')
    return {'snapshot_id':snapshot_id,'schema_objects_verified':schema_objects,
        'row_counts_verified':True,'reference_integrity':True,'sensitive_value_matches':0,
        'idempotent_import':True,'ready_for_application_switch':True,'pending_gates':[]}
async def main():
    current,candidate=target_settings(settings,snapshot_id)
    conn=await aiomysql.connect(host=settings.MYSQL_HOST,port=settings.MYSQL_PORT,user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,charset='utf8mb4',connect_timeout=5,autocommit=False)
    try:
        if action=='measure':
            result=await measure(conn,settings,snapshot_id)
            await verify_current_schema(conn,snapshot,current)
            result['production_schema_verified']=True
        elif action=='create': result=await create(conn,settings,snapshot_id)
        elif action=='import':
            async def initialize_observer(cur, candidate):
                fields='id,username,display_name,password_hash,role,password_is_temporary'
                await cur.execute('INSERT INTO `'+candidate['PlatformData']+'`._users ('+fields+') SELECT '+fields+' FROM `'+current['PlatformData']+'`._users WHERE username=%s',('observer@staging',))
                if cur.rowcount!=1:raise SnapshotError('observer_initialization_failed')
            result=await import_rows(conn,settings,snapshot,before_commit=initialize_observer)
            result['observer_initialized']=True
            result['pending_gates']=['target_verification','projection_rebuild']
        else:result=await verify_target(conn,settings,snapshot,current,candidate)
        print(json.dumps({'ok':True,'result':result}))
    finally:conn.close()
try:asyncio.run(main())
except SnapshotError as exc:print(json.dumps({'ok':False,'reason':str(exc),'diagnostics':exc.diagnostics}))
except Exception:print(json.dumps({'ok':False,'reason':'staging_candidate_job_failed'}))
'''
    return code, {name:hashlib.sha256(source.encode()).hexdigest() for name,source in modules.items()}


def run_job(backend, code, name, *, snapshot=None):
    args=['docker','run','--rm','-i','--name',name,'--network','binhu-staging_internal',
        '--label','binhu.environment=staging','--label','binhu.snapshot-job=true',
        '--memory','1g','--cpus','1','--pids-limit','128','--read-only',
        '--tmpfs','/tmp:rw,noexec,nosuid,size=32m','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--env-file','/srv/binhu-environments/staging/backend.env',
        '--entrypoint','python',backend['Image'],'-c',code]
    try:
        output=command(args,stdin=json.dumps(snapshot,ensure_ascii=True) if snapshot is not None else None,timeout=300)
    except subprocess.TimeoutExpired:
        # Stop only this exact temporary job after proving its labels. It has
        # no state volume and never shares the application container's cgroup.
        matches=json.loads(command(['docker','inspect',name]))
        if len(matches)==1 and matches[0]['Config']['Labels'].get('binhu.snapshot-job')=='true':
            command(['docker','stop','--time','10',matches[0]['Id']])
        raise SnapshotError('staging_candidate_job_timeout') from None
    lines=[line for line in output.splitlines() if line.startswith('{"ok":')]
    if len(lines)!=1:raise SnapshotError('staging_candidate_envelope_invalid')
    envelope=json.loads(lines[0])
    if not envelope.get('ok'):
        reason=envelope.get('reason','')
        diagnostics=envelope.get('diagnostics')
        if not isinstance(diagnostics,dict):
            diagnostics={}
        raise SnapshotError(reason if re.fullmatch('[a-z_]{1,100}',reason) else 'staging_candidate_job_failed',
                            diagnostics=diagnostics)
    return envelope['result']


def execute(action,snapshot_id):
    import fcntl
    os.umask(0o077)
    database_names(snapshot_id)
    path=ROOT/snapshot_id
    safe_directory(path)
    snapshot_path=path/'snapshot.json'
    if snapshot_path.is_symlink() or snapshot_path.stat().st_size>128*1024**2:
        raise SnapshotError('snapshot_artifact_invalid')
    content=snapshot_path.read_bytes()
    expected=json.loads((path/'snapshot-sha256.json').read_text())['sha256']
    if hashlib.sha256(content).hexdigest()!=expected:
        raise SnapshotError('snapshot_artifact_hash_mismatch')
    snapshot=json.loads(content)
    if snapshot['report']['snapshot_id']!=snapshot_id:
        raise SnapshotError('snapshot_artifact_identity_mismatch')
    with (ROOT/'snapshot.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=preflight()
        backend,mysql=resources()
        attempt=path/('candidate-'+action+'-'+secrets.token_hex(6))
        attempt.mkdir(mode=0o700)
        private_json(attempt/'before.json',before)
        try:
            # schema measurement must precede the narrow database grants.
            if action=='create':
                code,_=program(snapshot_id,'measure')
                run_job(backend,code,'binhu-staging-snapshot-'+secrets.token_hex(6),snapshot=snapshot)
                command(['docker','exec','-i',mysql['Id'],'sh','-c',
                    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=socket -uroot --batch --skip-column-names'],
                    stdin=grant_sql(snapshot_id))
            code,hashes=program(snapshot_id,action)
            private_json(attempt/'code-hashes.json',hashes)
            result=run_job(backend,code,'binhu-staging-snapshot-'+secrets.token_hex(6),
                           snapshot=snapshot if action in {'measure','import','verify'} else None)
            after=preflight()
            if any(before[key]!=after[key] for key in ('container_id','started_at','restart_count')):
                raise SnapshotError('production_baseline_changed')
            private_json(attempt/'after.json',after)
            private_json(attempt/'result.json',result)
            return result
        except Exception as exc:
            reason=str(exc) if isinstance(exc,SnapshotError) else 'staging_candidate_operation_failed'
            failure={'reason':reason}
            diagnostics=getattr(exc,'diagnostics',{})
            if isinstance(diagnostics,dict):
                safe={}
                for key in ('domain','table_name'):
                    value=diagnostics.get(key)
                    if isinstance(value,str) and len(value)<=128:
                        safe[key]=value
                for key in ('missing_tables','extra_tables'):
                    value=diagnostics.get(key)
                    if isinstance(value,list) and all(isinstance(item,str) and len(item)<=128 for item in value):
                        safe[key]=value
                for key in ('expected_table_count','actual_table_count'):
                    value=diagnostics.get(key)
                    if type(value) is int and 0<=value<=10000:
                        safe[key]=value
                if diagnostics.get('schema_signature_mismatch') is True:
                    safe['schema_signature_mismatch']=True
                if safe:
                    failure['diagnostics']=safe
            exit_code=getattr(exc,'diagnostics',{}).get('exit_code')
            if type(exit_code) is int and -255<=exit_code<=255:
                failure['exit_code']=exit_code
            private_json(attempt/'failure.json',failure)
            raise SnapshotError(reason) from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('measure','create','import','verify'))
    parser.add_argument('--snapshot-id',required=True)
    args=parser.parse_args()
    try:print(json.dumps(execute(args.action,args.snapshot_id)))
    except (SnapshotError,OSError):raise SystemExit('candidate operation failed; inspect private fixed-code evidence') from None


if __name__=='__main__':main()
