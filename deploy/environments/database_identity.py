"""Measure/repair missing identity markers on already isolated environment DBs.

Existing, conflicting markers are never overwritten. Resource checks are made
before entering the backend, where credentials stay inside the container.
"""
from __future__ import annotations

import argparse
import json
import subprocess

ENVIRONMENTS = {'development': 'Dev_', 'staging': 'Staging_'}


def command(args, stdin=None):
    result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('identity operation failed; no secret diagnostics emitted')
    return result.stdout


def validate_resources(environment, backend, mysql, network, containers):
    if environment not in ENVIRONMENTS:
        raise ValueError('non-production environment required')
    project = 'binhu-' + environment
    network_name = project + '_internal'
    for container, service in ((backend, 'backend'), (mysql, 'environment-mysql')):
        labels = container['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != project or labels.get('com.docker.compose.service') != service:
            raise ValueError('container project identity mismatch')
        if labels.get('binhu.environment') != environment or not container['State']['Running']:
            raise ValueError('container environment identity mismatch')
        if set(container['NetworkSettings']['Networks']) != {network_name}:
            raise ValueError('unexpected environment network')
        if container['HostConfig'].get('NetworkMode') == 'host' or container['HostConfig'].get('Privileged'):
            raise ValueError('unsafe container mode')
    data = [m for m in mysql['Mounts'] if m['Destination'] == '/var/lib/mysql']
    if len(data) != 1 or data[0]['Type'] != 'volume' or data[0]['Name'] != project + '_mysql':
        raise ValueError('unexpected database storage')
    members = network.get('Containers') or {}
    if any(not c.get('Name', '').startswith(project + '-') for c in members.values()):
        raise ValueError('foreign network member')
    # Check stopped containers too: an old shadow or production container must
    # not refer to this database state even if it is currently offline.
    for other in containers:
        if other['Id'] == mysql['Id']:
            continue
        if any(m.get('Source') == data[0]['Source'] for m in other.get('Mounts', [])):
            raise ValueError('database storage shared by another container')


INNER = '''
import asyncio,json,re
import aiomysql
from config import settings
environment=ENVIRONMENT
apply=APPLY
domains=('ONLINE_DATA','ARCHIVE','DAILY_REPORT','PLATFORM','VISIT','DISPATCH','REGISTRY','WORKFLOW')
async def run():
    prefix={'development':'Dev_','staging':'Staging_'}[environment]
    if settings.APP_ENVIRONMENT!=environment or settings.MYSQL_HOST!='environment-mysql' or settings.MYSQL_PORT!=3306 or settings.MYSQL_USER!='environment_app':
        raise ValueError('target mismatch')
    names=[getattr(settings,'MYSQL_'+d+'_DB') for d in domains]
    if len(set(names))!=8 or any(not re.fullmatch(prefix+r'[A-Za-z0-9_]+',n) for n in names):
        raise ValueError('database mismatch')
    conn=await aiomysql.connect(host=settings.MYSQL_HOST,port=settings.MYSQL_PORT,user=settings.MYSQL_USER,password=settings.MYSQL_PASSWORD,connect_timeout=5,autocommit=False)
    try:
        async with conn.cursor() as cur:
            missing=[]
            for name in names:
                await cur.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s',(name,))
                if (await cur.fetchone())[0]!=1: raise ValueError('missing database')
                await cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='_environment_identity'",(name,))
                if (await cur.fetchone())[0]==0:
                    missing.append(name)
                else:
                    await cur.execute('SELECT id,environment FROM `'+name+'`._environment_identity')
                    if list(await cur.fetchall())!=[(1,environment)]: raise ValueError('conflicting marker')
            # A verified pre-existing OnlineData marker is the initialization anchor.
            if names[0] in missing: raise ValueError('anchor marker missing')
            if apply:
                for name in missing:
                    await cur.execute('CREATE TABLE `'+name+'`._environment_identity (id INT PRIMARY KEY, environment VARCHAR(32) NOT NULL)')
                    await cur.execute('INSERT INTO `'+name+'`._environment_identity (id,environment) VALUES (1,%s)',(environment,))
                    await conn.commit()
            print(json.dumps({'environment':environment,'missing_before':missing,'markers_created':len(missing) if apply else 0,'all_markers_present':not missing or apply}))
    finally: conn.close()
asyncio.run(run())
'''


def run(environment, apply=False):
    if environment not in ENVIRONMENTS:
        raise ValueError('non-production target required')
    project = 'binhu-' + environment
    ids = command(['docker', 'ps', '-aq']).split()
    containers = json.loads(command(['docker', 'inspect', *ids]))
    by_name = {c['Name'].lstrip('/'): c for c in containers}
    backend = by_name[project + '-backend-1']
    mysql = by_name[project + '-environment-mysql-1']
    network = json.loads(command(['docker', 'network', 'inspect', project + '_internal']))[0]
    validate_resources(environment, backend, mysql, network, containers)
    source = INNER.replace('ENVIRONMENT', repr(environment), 1).replace('APPLY', repr(apply), 1)
    output = command(['docker', 'exec', '-i', backend['Id'], 'python', '-'], source)
    report = json.loads(output)
    if report.get('environment') != environment:
        raise ValueError('unexpected verification output')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('measure', 'apply', 'verify'))
    parser.add_argument('--environment', choices=ENVIRONMENTS, required=True)
    args = parser.parse_args()
    try:
        report = run(args.environment, args.action == 'apply')
        print(json.dumps(report))
        if args.action == 'verify' and not report['all_markers_present']:
            raise ValueError('database markers incomplete')
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        raise SystemExit('database identity gate failed; preserve current resources') from None


if __name__ == '__main__':
    main()
