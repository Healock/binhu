#!/usr/bin/env python3
"""Export a fresh synthetic Locust index after live, read-only shadow inspection."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from business_guard import DATABASE_RE, BusinessGuardError, validate_run_id
from inspect_business_runtime import collect_runtime, RuntimeInspectionError


class IndexExportError(ValueError):
    pass


def make_tasks():
    fixture = Path(__file__).resolve().parents[2] / 'load-tests' / 'fixture.py'
    spec = importlib.util.spec_from_file_location('business_index_fixture', fixture)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_tasks()


TASK_FIELDS = {'kind', 'ordinal', 'parser_type', 'row_key', 'source_id',
               'initial_revision', 'scenario', 'property_id', 'property_version',
               'source_revision', 'local_revision', 'hashes_match'}
PROPERTY_FIELDS = {'kind', 'source_ref', 'property_id', 'property_version'}


def _positive(value):
    return type(value) is int and value > 0


def build_index(records, run_id, snapshot_sha256):
    validate_run_id(run_id)
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot_sha256):
        raise IndexExportError('invalid runtime snapshot hash')
    expected = {t['ordinal']: t for t in make_tasks()}
    tasks, properties, source_ids, keys, property_ids = {}, {}, set(), set(), set()
    counts_seen = False
    expected_counts = dict(kind='counts', expectations=3600, sources=3600,
                           local_records=3600, properties=48, foreign_runs=0)
    for row in records:
        if not isinstance(row, dict):
            raise IndexExportError('record is not an object')
        if row.get('kind') == 'counts':
            if counts_seen or row != expected_counts:
                raise IndexExportError('fixture counts include missing or foreign records')
            counts_seen = True
        elif row.get('kind') == 'property' and set(row) == PROPERTY_FIELDS:
            refs = {f'shadow_loadtest:{run_id}:property:{n:02d}': n for n in range(1, 49)}
            n = refs.get(row['source_ref'])
            pid, version = row['property_id'], row['property_version']
            if n is None or n in properties or pid in property_ids or not _positive(pid) or version != 1:
                raise IndexExportError('foreign, duplicate or modified fixture property')
            properties[n] = {'property_id': pid, 'property_version': version}
            property_ids.add(pid)
        elif row.get('kind') == 'task' and set(row) == TASK_FIELDS:
            n, sid, key = row['ordinal'], row['source_id'], row['row_key']
            task = expected.get(n)
            if (not _positive(n) or task is None or n in tasks or not _positive(sid)
                    or sid in source_ids or not isinstance(key, str)
                    or not re.fullmatch(r'[0-9a-f]{32}', key) or key in keys):
                raise IndexExportError('invalid or duplicate fixture task identity')
            scenario = 'conflict' if task['conflict_group'] else task['state']
            revision = row['initial_revision']
            if (row['parser_type'] != task['parser_type'] or row['scenario'] != scenario
                    or not _positive(revision) or row['source_revision'] != revision
                    or row['local_revision'] != revision or row['hashes_match'] != 1):
                raise IndexExportError('fixture identity, revision or hash mismatch')
            tasks[n] = row
            source_ids.add(sid)
            keys.add(key)
        else:
            raise IndexExportError('unknown record kind or fields')
    if not counts_seen or set(tasks) != set(expected) or set(properties) != set(range(1, 49)):
        raise IndexExportError('index requires exactly 3600 tasks and 48 fixture properties')
    output = []
    for n, task in expected.items():
        row = tasks[n]
        selected = {'property_id': row['property_id'], 'property_version': row['property_version']}
        wanted = properties[task['property_index']] if task['state'] == 'pending_registration' else {
            'property_id': None, 'property_version': None}
        if selected != wanted:
            raise IndexExportError('registration selection does not match the fixture')
        start = ((task['property_index'] - 1) // 4) * 4 + 1
        output.append({key: row[key] for key in (
            'ordinal', 'parser_type', 'row_key', 'source_id', 'initial_revision', 'scenario',
            'property_id', 'property_version')})
        output[-1].update(community=task['community'], inspector=task['assigned_user'],
                          property_candidates=[dict(properties[i]) for i in range(start, start + 4)])
    return {'schema_version': 1, 'run_id': run_id, 'fictional_only': True,
            'runtime_snapshot_sha256': snapshot_sha256, 'tasks': output}


def build_query_command(container, database, run_id):
    validate_run_id(run_id)
    if DATABASE_RE.fullmatch(database) is None or not re.fullmatch(r'[a-zA-Z0-9_.-]+', container):
        raise IndexExportError('invalid shadow query target')
    sql = f"""
START TRANSACTION READ ONLY;
SELECT /*+ MAX_EXECUTION_TIME(10000) */ JSON_OBJECT('kind','counts',
 'expectations',(SELECT COUNT(*) FROM _shadow_business_expectations),
 'foreign_runs',(SELECT COUNT(*) FROM _shadow_business_expectations WHERE run_id<>'{run_id}'),
 'sources',(SELECT COUNT(*) FROM _online_source_rows),
 'local_records',(SELECT COUNT(*) FROM _local_source_records),
 'properties',(SELECT COUNT(*) FROM registry_properties));
SELECT /*+ MAX_EXECUTION_TIME(10000) */ JSON_OBJECT(
 'kind','task','ordinal',e.ordinal_no,'parser_type',e.parser_type,'row_key',e.row_key,
 'source_id',e.source_id,'initial_revision',e.initial_revision,'scenario',e.scenario,
 'property_id',e.property_id,'property_version',e.property_version,
 'source_revision',s.revision,'local_revision',l.revision,
 'hashes_match',IF(s.row_hash=l.content_hash AND s.row_hash<>'',1,0))
FROM _shadow_business_expectations e
JOIN _online_source_rows s ON s.id=e.source_id AND s.parser_type=e.parser_type AND s.row_key=e.row_key
JOIN _local_source_records l ON l.parser_type=s.parser_type AND l.business_key=s.row_key
 AND l.source_kind=s.source_kind AND l.source_ref=s.source_ref
WHERE e.run_id='{run_id}' AND s.source_kind='local_table'
 AND s.archived_at IS NULL AND l.status='active' AND l.archived_at IS NULL
ORDER BY e.ordinal_no LIMIT 3601;
SELECT /*+ MAX_EXECUTION_TIME(10000) */ JSON_OBJECT('kind','property',
 'source_ref',source_ref,'property_id',id,'property_version',current_version)
FROM registry_properties WHERE source_type='shadow_loadtest' AND status='active'
 AND source_ref LIKE 'shadow_loadtest:{run_id}:property:%' ORDER BY id LIMIT 49;
COMMIT;
"""
    inner = ('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot -N -B --raw '
             f'--default-character-set=utf8mb4 {shlex.quote(database)} -e {shlex.quote(sql)}')
    return ['docker', 'exec', container, 'sh', '-c', inner]


def write_index(path, payload, root):
    target = Path(path).resolve()
    try:
        artifacts = (Path(root) / 'artifacts').resolve()
        artifacts.relative_to(Path(root).resolve())
        target.relative_to(artifacts)
        if target.suffix != '.json':
            raise ValueError('not JSON')
        with target.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (ValueError, OSError) as exc:
        raise IndexExportError('index must be a new JSON file inside project artifacts') from exc


def export_index(root, run_id, output):
    root = Path(root).resolve()
    snapshot = collect_runtime(root, run_id=run_id, phase='verify')
    docker = snapshot['docker_identity']
    containers = [r for r in docker['containers']
                  if r['Config']['Labels'].get('com.docker.compose.service') == 'derived-mysql']
    if len(containers) != 1:
        raise IndexExportError('expected exactly one verified MySQL container')
    command = build_query_command(containers[0]['Name'].lstrip('/'), docker['databases']['online'], run_id)
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                encoding='utf-8', timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IndexExportError('read-only index query failed or timed out') from exc
    if result.returncode:
        raise IndexExportError('read-only index query failed; database output withheld')
    try:
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as exc:
        raise IndexExportError('invalid query record format; output withheld') from exc
    payload = build_index(records, run_id, snapshot['sha256'])
    write_index(output, payload, root)
    return {'status': 'exported', 'run_id': run_id, 'tasks': len(payload['tasks']),
            'runtime_snapshot_sha256': snapshot['sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(export_index(Path.cwd(), args.run_id, args.output)))
    except (IndexExportError, RuntimeInspectionError, BusinessGuardError) as exc:
        print(f'index export stopped: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
