import copy
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'kafka-shadow'))
import export_business_index as exporter

RUN = 'KSHADOW-index01'


def records():
    rows = [dict(kind='counts', expectations=3600, sources=3600, local_records=3600,
                 properties=48, foreign_runs=0)]
    for task in exporter.make_tasks():
        n = task['ordinal']
        pending = task['state'] == 'pending_registration'
        rows.append(dict(kind='task', ordinal=n, parser_type=task['parser_type'],
                         row_key=f'{n:032x}', source_id=n, initial_revision=1,
                         scenario='conflict' if task['conflict_group'] else task['state'],
                         property_id=task['property_index'] if pending else None,
                         property_version=1 if pending else None,
                         source_revision=1, local_revision=1, hashes_match=1))
    for n in range(1, 49):
        rows.append(dict(kind='property', source_ref=f'shadow_loadtest:{RUN}:property:{n:02d}',
                         property_id=n, property_version=1))
    return rows


def test_full_index_reconstructs_only_fictional_workload_metadata():
    result = exporter.build_index(records(), RUN, 'a' * 64)
    assert len(result['tasks']) == 3600
    assert result['runtime_snapshot_sha256'] == 'a' * 64
    assert result['tasks'][0]['community'] == '压测社区01'
    assert result['tasks'][0]['inspector'] == '压测组员01'
    assert len(result['tasks'][0]['property_candidates']) == 4
    assert sum(t['scenario'] == 'conflict' for t in result['tasks']) == 30
    assert 'identity_number' not in json.dumps(result)
    assert 'original_address' not in json.dumps(result)


@pytest.mark.parametrize('mutation', [
    lambda r: r.pop(0),
    lambda r: r.append(copy.deepcopy(r[1])),
    lambda r: r[1].update(source_id=2),
    lambda r: r[1].update(row_key=r[2]['row_key']),
    lambda r: r[1].update(scenario='completed'),
    lambda r: r[1].update(parser_type='unknown'),
    lambda r: r[1].update(source_revision=2),
    lambda r: r[1].update(local_revision=2),
    lambda r: r[1].update(hashes_match=0),
    lambda r: r[1].update(identity_number='not-exportable'),
    lambda r: r[-1].update(source_ref='shadow_loadtest:other:property:48'),
    lambda r: r[-1].update(property_id=1),
    lambda r: r[511].update(property_id=48),
    lambda r: r[0].update(sources=3601),
    lambda r: r[0].update(local_records=3601),
    lambda r: r[0].update(expectations=3601),
    lambda r: r[0].update(foreign_runs=1),
    lambda r: r[0].update(properties=49),
])
def test_incomplete_foreign_stale_or_unexpected_data_is_rejected(mutation):
    rows = records()
    mutation(rows)
    with pytest.raises(exporter.IndexExportError):
        exporter.build_index(rows, RUN, 'a' * 64)


def test_query_is_readonly_bounded_and_does_not_select_body():
    command = exporter.build_query_command('binhu-kafka-shadow-index-derived-mysql-1',
                                           'KShadow_index', RUN)
    assert command[:2] == ['docker', 'exec']
    sql = command[-1]
    assert 'START TRANSACTION READ ONLY' in sql
    assert 'MAX_EXECUTION_TIME(10000)' in sql
    assert 'values_json' not in sql
    assert 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD"' in sql
    assert '_shadow_business_expectations' in sql
    assert '_online_source_projection' not in sql


@pytest.mark.parametrize('database,run', [('OnlineData', RUN), ('KShadow_index', "x';DELETE")])
def test_query_rejects_unscoped_identifiers(database, run):
    with pytest.raises(ValueError):
        exporter.build_query_command('mysql', database, run)


def test_writer_refuses_overwrite_or_escape(tmp_path):
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    target = artifacts / 'runtime.json'
    exporter.write_index(target, {'tasks': []}, tmp_path)
    with pytest.raises(exporter.IndexExportError):
        exporter.write_index(target, {}, tmp_path)
    assert json.loads(target.read_text()) == {'tasks': []}
    with pytest.raises(exporter.IndexExportError):
        exporter.write_index(tmp_path / 'outside.json', {}, tmp_path)


def snapshot():
    return {'sha256': 'a' * 64, 'docker_identity': {
        'databases': {'online': 'KShadow_index'},
        'containers': [{'Name': '/binhu-kafka-shadow-index-derived-mysql-1',
                        'Config': {'Labels': {'com.docker.compose.service': 'derived-mysql'}}}]}}


def test_orchestration_requires_fresh_inspection_before_readonly_query(monkeypatch, tmp_path):
    calls = []
    def inspect(root, **kwargs):
        calls.append(('inspect', kwargs))
        return snapshot()
    def run(command, **kwargs):
        assert calls[0][0] == 'inspect'
        assert kwargs['timeout'] == 30
        assert kwargs['cwd'] == tmp_path
        calls.append(('query', command))
        return SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps(r) for r in records()))
    monkeypatch.setattr(exporter, 'collect_runtime', inspect)
    monkeypatch.setattr(exporter.subprocess, 'run', run)
    (tmp_path / 'artifacts').mkdir()
    output = tmp_path / 'artifacts/runtime.json'
    result = exporter.export_index(tmp_path, RUN, output)
    assert calls[0][1] == {'run_id': RUN, 'phase': 'verify'}
    assert result['tasks'] == 3600
    assert len(json.loads(output.read_text(encoding='utf-8'))['tasks']) == 3600


def test_failed_preflight_never_queries_database_or_writes_index(monkeypatch, tmp_path):
    def inspect(*args, **kwargs):
        raise exporter.RuntimeInspectionError('scope mismatch')
    def forbidden(*args, **kwargs):
        pytest.fail('query must not run after failed inspection')
    monkeypatch.setattr(exporter, 'collect_runtime', inspect)
    monkeypatch.setattr(exporter.subprocess, 'run', forbidden)
    with pytest.raises(exporter.RuntimeInspectionError):
        exporter.export_index(tmp_path, RUN, tmp_path / 'artifacts/runtime.json')
    assert not (tmp_path / 'artifacts/runtime.json').exists()


def test_query_error_does_not_disclose_mysql_output(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, 'collect_runtime', lambda *a, **kw: snapshot())
    monkeypatch.setattr(exporter.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='sensitive database output', stderr='secret password'))
    with pytest.raises(exporter.IndexExportError, match='output withheld') as error:
        exporter.export_index(tmp_path, RUN, tmp_path / 'artifacts/runtime.json')
    assert 'sensitive' not in str(error.value) and 'secret' not in str(error.value)


def test_artifacts_symlink_cannot_escape_project(tmp_path):
    project, outside = tmp_path / 'project', tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    try:
        (project / 'artifacts').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(exporter.IndexExportError):
        exporter.write_index(project / 'artifacts/runtime.json', {}, project)
    assert not (outside / 'runtime.json').exists()


def test_runtime_snapshot_rejects_artifacts_symlink(tmp_path):
    from inspect_business_runtime import write_snapshot
    project, outside = tmp_path / 'project', tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    try:
        (project / 'artifacts').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(exporter.RuntimeInspectionError):
        write_snapshot(project / 'artifacts/runtime.json', {}, project)
    assert not (outside / 'runtime.json').exists()
