import sys
import types
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

from routers import mobile_tasks
from routers.mobile_tasks import AnalysisTaskSearch, TaskSearch
from services.parsers import PARSER_REGISTRY
from services.permissions import ONLINE_RAW_EDIT


class _RedisClient:
    async def ping(self):
        return True

    async def aclose(self):
        return None


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = connection.rows
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, *_args):
        self.statements.append(str(sql))
        params = _args[0] if _args else ()
        if "SELECT source.id,source.physical_row,source.row_key,source.revision " in str(sql):
            self.rows = [
                self.connection.source_locators[source_id]
                for source_id in params[1:]
                if source_id in self.connection.source_locators
            ]
        if "SELECT row_key,COUNT(*) FROM _online_source_rows" in str(sql):
            self.rows = self.connection.source_count_rows
        if "SELECT source.row_key,COUNT(*) FROM _online_source_rows" in str(sql):
            self.rows = [
                (row_key, count)
                for row_key, count in self.connection.source_count_rows
                if row_key in params[1:]
            ]
        return None

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, rows, source_count_rows=None, source_locators=None):
        self.rows = rows
        self.source_count_rows = source_count_rows or []
        self.source_locators = source_locators or {}
        self.cursors = []

    def cursor(self):
        cursor = _Cursor(self)
        self.cursors.append(cursor)
        return cursor


def _install_redis(monkeypatch):
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.from_url = lambda *_args, **_kwargs: _RedisClient()
    redis_package = types.ModuleType("redis")
    redis_package.__path__ = []
    redis_package.asyncio = redis_asyncio
    monkeypatch.setitem(sys.modules, "redis", redis_package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)


def _record(task_id, source_id, revision, values):
    return {
        "task_id": task_id,
        "source_id": source_id,
        "revision": revision,
        "event_id": f"event-{source_id}",
        "updated_at": "2026-11-05T00:00:00Z",
        "values": values,
    }


@pytest.fixture
def read_model(monkeypatch):
    _install_redis(monkeypatch)
    monkeypatch.setattr(mobile_tasks.settings, "TASK_READ_MODEL_ENABLED", True)
    monkeypatch.setattr(mobile_tasks.settings, "TASK_READ_MODEL_RUN_ID", "dev-read-test")
    monkeypatch.setattr(mobile_tasks.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 40)
    monkeypatch.setattr(mobile_tasks.settings, "APP_ENVIRONMENT", "development")
    async def no_indexed_state_counts(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mobile_tasks, "_indexed_task_state_counts", no_indexed_state_counts)
    import services.task_read_model as task_read_model

    monkeypatch.setattr(task_read_model, "require_task_read_model_ready", _ready)
    return task_read_model


async def _ready(*_args):
    return None


@pytest.mark.asyncio
async def test_mobile_task_scan_uses_shared_batched_snapshot_decoder(read_model, monkeypatch):
    parser = mobile_tasks.get_parser("全链条")
    first_values = {column: "" for column in parser.COLUMNS}
    first_values.update({
        "社区": "社区甲", "核查人": "组员甲", "姓名": "对象甲",
        "身份证号": "合成证件号", "电话号码": "合成号码",
        "下发日期": "2026-11-05", "核查结果": "在吴",
    })
    second_values = {**first_values, "姓名": "对象乙"}
    records_by_task = {
        f"{parser.table_name}:17": _record(
            f"{parser.table_name}:17", 117, 3, first_values,
        ),
        f"{parser.table_name}:18": _record(
            f"{parser.table_name}:18", 118, 2, second_values,
        ),
    }
    calls = {"pages": [], "groups": []}
    first_values = records_by_task[f"{parser.table_name}:17"]["values"]
    row_key = parser.make_row_key(first_values)

    async def query_groups(_client, *args):
        calls["pages"].append(args)
        return {"total": 1, "group_tokens": ["a" * 64]}

    async def read_groups(_client, _environment, _run_id, _parser_type, tokens):
        calls["groups"].append(tokens)
        return [{
            "row_key": row_key,
            "parser_type": parser.table_name,
            "source_count": 2,
            "conflict": False,
            "values": first_values,
            "sources": [records_by_task[f"{parser.table_name}:17"], records_by_task[f"{parser.table_name}:18"]],
        }]

    monkeypatch.setattr(read_model, "query_task_read_model_group_ids", query_groups)
    monkeypatch.setattr(read_model, "read_task_read_model_group_records", read_groups)

    result = await mobile_tasks._scan_task_read_model_records(
        _RedisClient(),
        parser=parser,
        allowed_communities=["社区甲"],
        scope="mine",
        inspector="组员甲",
        keyword="对象",
        filters={"核查结果": ["在吴"]},
        environment="development",
        run_id="dev-read-test",
    )

    assert [record["task_id"] for record in result] == [
        f"{parser.table_name}:17", f"{parser.table_name}:18",
    ]
    assert calls["pages"][0][3] == {"核查结果": ["在吴"], "核查人": ["组员甲"]}
    assert calls["pages"][0][5] == "对象"
    assert calls["pages"][0][6:] == (0, 400)
    assert calls["groups"] == [["a" * 64]]


@pytest.mark.asyncio
async def test_mobile_read_model_rejects_scope_mismatch(read_model, monkeypatch):
    monkeypatch.setattr(
        mobile_tasks,
        "_scan_task_read_model_records",
        _scan_records_result([
            _record("t_fullchain:17", 117, 3, {"社区": "越权社区", "姓名": "合成对象"}),
        ]),
    )
    context = {"admin_mode": False, "community_values": ["授权社区"], "name": "组员甲"}

    with pytest.raises(HTTPException) as error:
        await mobile_tasks._task_read_model_records(
            _Connection([(117, 17, "row-17", 3, "hash-17")]),
            "全链条",
            context,
            scope="community",
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"error": "projection_pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redis_records,locator_rows",
    [
        ([_record("t_fullchain:17", 117, 3, {"社区": "授权社区"})], []),
        ([_record("t_fullchain:17", 117, 3, {"社区": "授权社区"})],
         [(117, 17, "row-17", 4, "hash-17")]),
    ],
)
async def test_mobile_read_model_rejects_missing_or_stale_source_snapshot(
    read_model, monkeypatch, redis_records, locator_rows,
):
    monkeypatch.setattr(
        mobile_tasks,
        "_scan_task_read_model_records",
        _scan_records_result(redis_records),
    )
    context = {"admin_mode": False, "community_values": ["授权社区"], "name": "组员甲"}

    with pytest.raises(HTTPException) as error:
        await mobile_tasks._task_read_model_records(
            _Connection(locator_rows), "全链条", context, scope="community",
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"error": "projection_pending"}


@pytest.mark.asyncio
async def test_mobile_read_model_groups_sources_and_detects_value_conflict(read_model, monkeypatch):
    parser = mobile_tasks.get_parser("全链条")
    first_values = {column: "" for column in parser.COLUMNS}
    first_values.update({
        "社区": "授权社区", "核查人": "组员甲", "姓名": "对象甲",
        "身份证号": "合成证件号", "电话号码": "合成号码",
        "下发日期": "2026-11-05",
    })
    second_values = {**first_values, "姓名": "对象乙"}
    row_key = parser.make_row_key(first_values)
    monkeypatch.setattr(
        mobile_tasks,
        "_scan_task_read_model_records",
        _scan_records_result([
            {**_record("t_fullchain:17", 117, 3, first_values), "row_key": row_key},
            {**_record("t_fullchain:18", 118, 2, second_values), "row_key": row_key},
        ]),
    )
    context = {"admin_mode": False, "community_values": ["授权社区"], "name": "组员甲"}

    rows = await mobile_tasks._task_read_model_records(
        _Connection([
            (117, 17, row_key, 3, "hash-17"),
            (118, 18, row_key, 2, "hash-18"),
        ], source_count_rows=[(row_key, 2)]),
        "全链条",
        context,
        scope="community",
    )

    assert len(rows) == 1
    assert rows[0]["source_count"] == 2
    assert rows[0]["conflict"] is True
    assert {source["source_id"] for source in rows[0]["sources"]} == {117, 118}


@pytest.mark.asyncio
async def test_mobile_read_model_rejects_incomplete_cross_community_group(
    read_model, monkeypatch,
):
    monkeypatch.setattr(
        mobile_tasks,
        "_scan_task_read_model_records",
        _scan_records_result([
            _record("t_fullchain:17", 117, 3, {"社区": "授权社区", "姓名": "对象甲"}),
        ]),
    )
    context = {"admin_mode": False, "community_values": ["授权社区"], "name": "组员甲"}

    with pytest.raises(HTTPException) as error:
        await mobile_tasks._task_read_model_records(
        _Connection(
            [(117, 17, "business-row", 3, "hash-17")],
            source_count_rows=[("business-row", 2)],
        ),
        "全链条",
        context,
        scope="community",
    )
    assert error.value.status_code == 503
    assert error.value.detail == {"error": "projection_pending"}


def _scan_records_result(records):
    async def result(*_args, **_kwargs):
        return records
    return result


def _task_record(parser_type, source_id, row_key, values, *, source_count=1, conflict=False):
    parser = mobile_tasks.get_parser(parser_type)
    record = _record(f"{parser.table_name}:{source_id}", source_id, 1, values)
    record.update({
        "parser_type": parser_type,
        "row_key": row_key,
        "source_count": source_count,
        "conflict": conflict,
        "sources": [record],
    })
    return record


def _assignment_group(record):
    return {
        "canonical_source": record,
        "values": record["values"],
        "row_key": record["row_key"],
        "source_count": record["source_count"],
        "conflict": record["conflict"],
        "sources": record["sources"],
    }


def _assignment_group_pages(groups, page_size=400):
    async def iterator(*_args, **_kwargs):
        for offset in range(0, len(groups), page_size):
            yield groups[offset:offset + page_size]
    return iterator


def _install_task_view_states(monkeypatch, records):
    states = {}
    for record in records:
        task_id = str(record["task_id"])
        address = {
            "status": str(record.get("address_match_status") or "unmatched"),
            "candidate": None,
            "candidates": [],
        }
        states[task_id] = {
            "address_match": {"state": address},
            "watch": {"state": {"status": "none", "watch_marks": []}},
            "qmf": {"state": {"state": "not_scanned", "status": "not_scanned"}},
            "residence": {"state": {"state": "pending", "status": "pending"}},
            "registration": {"state": {"status": "not_applicable"}},
            "review": {"state": {"state": "not_applicable", "events": []}},
            "task_graph": {"state": {"state": "not_applicable", "blocked": False}},
        }

    async def read(_records):
        return states

    monkeypatch.setattr(mobile_tasks, "_read_task_view_states_for_records", read)
    return states


@pytest.mark.asyncio
async def test_mobile_home_counts_task_states_from_read_model(read_model, monkeypatch):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    unchecked_values = {column: "" for column in parser.COLUMNS}
    unchecked_values.update({
        parser.COMMUNITY_COLUMN: "授权社区", "核查结果": "",
        "身份证号": "合成证件117", "联系方式": "合成号码117",
    })
    completed_values = {column: "" for column in parser.COLUMNS}
    completed_values.update({
        parser.COMMUNITY_COLUMN: "授权社区", "核查结果": "在吴",
        "身份证号": "合成证件118", "联系方式": "合成号码118",
    })
    unchecked = _record(f"{parser.table_name}:17", 117, 3, unchecked_values)
    completed = _record(f"{parser.table_name}:18", 118, 2, completed_values)
    groups = {
        "a" * 64: {
            "row_key": parser.make_row_key(unchecked_values),
            "values": unchecked_values,
            "sources": [unchecked], "source_count": 1, "conflict": False,
        },
        "b" * 64: {
            "row_key": parser.make_row_key(completed_values),
            "values": completed_values,
            "sources": [completed], "source_count": 1, "conflict": False,
        },
    }
    table_types = {parser_cls.table_name: key for key, parser_cls in PARSER_REGISTRY.items()}
    query_calls = []

    async def flow_context(_conn, _user):
        return {
            "admin_mode": True, "community_values": None, "name": "管理员",
            "position": "管理员", "community": "全所",
        }

    async def query_groups(_client, environment, run_id, table_name, filters,
                           communities, search, offset, limit):
        query_calls.append((table_name, communities, offset, limit))
        assert environment == "development"
        assert run_id == "dev-read-test"
        assert filters == {} and search is None
        if table_types[table_name] != parser_type:
            return {"total": 0, "group_tokens": []}
        tokens = ["a" * 64, "b" * 64]
        return {"total": 2, "group_tokens": tokens[offset:offset + limit]}

    async def read_groups(_client, _environment, _run_id, _table_name, tokens):
        return [groups[token] for token in tokens]

    async def business_date(_cur):
        import datetime
        return datetime.date(2026, 11, 5)

    async def overview(*_args, **_kwargs):
        return {"exists": False}

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(read_model, "query_task_read_model_group_ids", query_groups)
    monkeypatch.setattr(read_model, "read_task_read_model_group_records", read_groups)
    monkeypatch.setattr(mobile_tasks, "get_business_date", business_date)
    monkeypatch.setattr(mobile_tasks, "get_online_overview", overview)
    locators = {
        117: (117, 17, parser.make_row_key(unchecked_values), 3),
        118: (118, 18, parser.make_row_key(completed_values), 2),
    }
    conn = _Connection(
        [("2026-11-05T00:00:00",)],
        source_count_rows=[
            (parser.make_row_key(unchecked_values), 1),
            (parser.make_row_key(completed_values), 1),
        ],
        source_locators=locators,
    )

    result = await mobile_tasks.get_mobile_task_home(
        scope="all", user={"id": 1, "role": "admin", "permissions": []}, conn=conn,
    )

    business = next(item for item in result["businesses"] if item["parser_type"] == parser_type)
    assert business["unchecked"] == 1
    assert business["completed"] == 1
    assert business["pending"] == 1
    assert result["personal"]["pending"] == 1
    assert result["community"]["pending"] == 1
    assert query_calls
    assert all("_online_source_projection" not in sql for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_mobile_filter_options_are_derived_from_read_model_and_avoid_projection(
    read_model, monkeypatch,
):
    parser_type = "全链条"
    parser = mobile_tasks.get_parser(parser_type)
    first_values = {
        parser.COMMUNITY_COLUMN: "社区甲",
        "姓名": "合成对象甲",
        "核查人": "民警甲",
        "核查结果": "已登记",
    }
    second_values = {
        parser.COMMUNITY_COLUMN: "社区乙",
        "姓名": "合成对象乙",
        "核查人": "民警乙",
        "核查结果": "未登记",
    }
    records = [
        _task_record(parser_type, 117, "filter-row-a", first_values),
        _task_record(parser_type, 118, "filter-row-b", second_values),
    ]
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    requested_state_filters = []

    async def group_pages(*_args, **_kwargs):
        requested_state_filters.append(_kwargs.get("state_filters"))
        yield records[:1]
        yield records[1:]

    states = _install_task_view_states(monkeypatch, records)
    states[records[0]["task_id"]]["watch"]["state"]["watch_marks"] = [{
        "category_id": 1, "name": "重点类别甲", "color": "red", "alert_level": "high",
    }]
    states[records[1]["task_id"]]["watch"]["state"]["watch_marks"] = [{
        "category_id": 2, "name": "重点类别乙", "color": "blue", "alert_level": "normal",
    }]
    states[records[0]["task_id"]]["address_match"]["state"].update({
        "status": "confirmed", "candidate": {"name": "小区甲"},
    })

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_task_read_model_group_pages", group_pages)
    conn = _Connection([])

    result = await mobile_tasks.get_mobile_task_filter_options(
        parser_type=parser_type,
        scope="all",
        community=["社区甲"],
        small_community=["小区甲"],
        match_status=["confirmed"],
        result=["已登记"],
        review_stage="all",
        user={"id": 1, "permissions": []},
        conn=conn,
    )

    assert result["source_ready"] is True
    assert result["communities"] == [
        {"value": "社区乙", "label": "社区乙", "count": 1},
        {"value": "社区甲", "label": "社区甲", "count": 1},
    ]
    assert result["inspectors"] == [{"value": "民警甲", "label": "民警甲", "count": 1}]
    assert any(item["value"] == "已登记" for item in result["results"])
    assert result["small_communities"] == [{
        "value": "小区甲", "label": "小区甲", "count": 1,
    }]
    assert result["match_statuses"] == [{
        "value": "confirmed", "label": "confirmed", "count": 1,
    }]
    assert result["watch_categories"] == [
        {"value": 2, "label": "重点类别乙", "color": "blue", "alert_level": "normal", "count": 1},
        {"value": 1, "label": "重点类别甲", "color": "red", "alert_level": "high", "count": 1},
    ]
    assert requested_state_filters == [
        None,
        {
            "address_match.small_community_name": ["小区甲"],
            "address_match.status": ["confirmed"],
        },
    ]
    assert all("_online_source_projection" not in sql for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_mobile_list_filters_and_returns_qmf_and_residence_states_from_redis(
    read_model, monkeypatch,
):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    record = _task_record(
        parser_type, 117, "qmf-row",
        {parser.COMMUNITY_COLUMN: "社区甲", "核查结果": "在吴", "地址": "合成地址"},
    )
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    requested_state_filters = []

    async def task_group_pages(*_args, **_kwargs):
        requested_state_filters.append(_kwargs.get("state_filters"))
        yield [record]

    async def view_states(_records):
        return {record["task_id"]: {
            "address_match": {"state": {"status": "matched", "candidate": None}},
            "watch": {"state": {"status": "none", "watch_marks": []}},
            "qmf": {"state": {
                "state": "completed_mismatch", "status": "completed_mismatch",
                "platform_result": "离开不返吴", "result": "在吴",
            }},
            "residence": {"state": {"state": "registered", "status": "registered"}},
            "registration": {"state": {"status": "not_applicable"}},
            "review": {"state": {"state": "not_applicable", "events": []}},
        }}

    indexed_dimensions = {}

    async def indexed_counts(_parser, _context, *, dimensions, **_kwargs):
        indexed_dimensions.update(dimensions)
        return {
            "qmf.state": {value: int(value == "completed_mismatch") for value in dimensions["qmf.state"]},
            "registration.status": {"review_required": 0},
        }

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_task_read_model_group_pages", task_group_pages)
    monkeypatch.setattr(mobile_tasks, "_read_task_view_states_for_records", view_states)
    monkeypatch.setattr(mobile_tasks, "_indexed_task_state_counts", indexed_counts)
    conn = _Connection([])

    result = await mobile_tasks._list_mobile_tasks_data_from_read_model(
        parser_type,
        TaskSearch(scope="all", status="all", qmf_feedback_states=["completed_mismatch"]),
        {"id": 1, "permissions": []},
        conn,
    )

    assert result["total"] == 1
    assert result["data"][0]["qmf_status"]["state"] == "completed_mismatch"
    assert result["data"][0]["residence_status"]["state"] == "registered"
    assert requested_state_filters == [None, {"qmf.state": ["completed_mismatch"]}]
    assert result["facets"]["qmf_feedback_counts"]["completed_mismatch"] == 1
    assert result["facets"]["qmf_feedback_counts"]["not_scanned"] == 0
    assert indexed_dimensions["qmf.state"] == list(result["facets"]["qmf_feedback_counts"])
    assert all("_qmf_status_snapshots" not in sql and "_residence_registration_status" not in sql
               for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_analysis_filter_options_are_derived_from_read_model(read_model, monkeypatch):
    parser_type = "全链条"
    parser = mobile_tasks.get_parser(parser_type)
    record = _task_record(
        parser_type, 117, "analysis-filter-row",
        {parser.COMMUNITY_COLUMN: "社区乙", "姓名": "合成对象乙", "核查人": "民警乙"},
    )
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    async def group_pages(*_args, **_kwargs):
        yield [record]

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_task_read_model_group_pages", group_pages)
    _install_task_view_states(monkeypatch, [record])
    conn = _Connection([])

    result = await mobile_tasks.get_mobile_task_analysis_filter_options(
        parser_type=[parser_type], community=[], review_stage="all",
        user={"id": 1, "permissions": []}, conn=conn,
    )

    assert result["source_ready"] is True
    assert result["communities"] == [{"value": "社区乙", "label": "社区乙", "count": 1}]
    assert result["inspectors"] == [{"value": "民警乙", "label": "民警乙", "count": 1}]
    assert all("_online_source_projection" not in sql for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_mobile_list_streams_groups_counts_facets_and_keeps_only_requested_page(
    read_model, monkeypatch,
):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    records = [
        _task_record(parser_type, 117, "row-a", {
            parser.COMMUNITY_COLUMN: "社区甲", "核查结果": "在吴", "地址": "合成地址甲",
        }),
        _task_record(parser_type, 118, "row-b", {
            parser.COMMUNITY_COLUMN: "社区甲", "核查结果": "在吴", "地址": "合成地址乙",
        }),
    ]
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    requested_state_filters = []

    async def group_pages(*_args, **_kwargs):
        requested_state_filters.append(_kwargs.get("state_filters"))
        for record in records:
            yield [record]

    async def view_states(page_records):
        first = page_records[0]
        qmf = "completed_mismatch" if first["source_id"] == 117 else "not_scanned"
        registration = "review_required" if first["source_id"] == 117 else "not_applicable"
        return {first["task_id"]: {
            "address_match": {"state": {"status": "matched", "candidate": None}},
            "watch": {"state": {"status": "none", "watch_marks": []}},
            "qmf": {"state": {"state": qmf}},
            "residence": {"state": {"status": "registered"}},
            "registration": {"state": {"status": registration}},
            "review": {"state": {"state": "not_applicable"}},
        }}

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_task_read_model_group_pages", group_pages)
    monkeypatch.setattr(mobile_tasks, "_read_task_view_states_for_records", view_states)

    result = await mobile_tasks._list_mobile_tasks_data_from_read_model(
        parser_type,
        TaskSearch(
            scope="all", status="all", qmf_feedback_states=["completed_mismatch"],
            page=1, page_size=1,
        ),
        {"id": 1, "permissions": []},
        _Connection([]),
    )

    assert result["total"] == 1
    assert len(result["data"]) == 1
    assert result["data"][0]["row_key"] == "row-a"
    assert result["facets"]["total"] == 2
    assert result["facets"]["registration_review_count"] == 1
    assert result["facets"]["qmf_feedback_counts"]["completed_mismatch"] == 1
    assert result["facets"]["qmf_feedback_counts"]["not_scanned"] == 1
    assert requested_state_filters == [None, {"qmf.state": ["completed_mismatch"]}]


@pytest.mark.asyncio
async def test_analysis_list_streams_groups_with_exact_stage_total_and_facets(read_model, monkeypatch):
    parser_type = "全链条"
    parser = mobile_tasks.get_parser(parser_type)
    records = [
        _task_record(parser_type, 117, "analysis-a", {
            parser.COMMUNITY_COLUMN: "社区甲", "核查人": "民警甲", "核查结果": "无法核实",
        }),
        _task_record(parser_type, 118, "analysis-b", {
            parser.COMMUNITY_COLUMN: "社区甲", "核查人": "民警甲", "核查结果": "无法核实",
        }),
    ]
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    requested_state_filters = []

    async def group_pages(*_args, **_kwargs):
        requested_state_filters.append(_kwargs.get("state_filters"))
        for record in records:
            yield [record]

    async def view_states(page_records):
        record = page_records[0]
        stage = "initial_pending" if record["source_id"] == 117 else "deep_pending"
        return {record["task_id"]: {
            "review": {"state": {"state": stage}},
            "address_match": {"state": {"status": "unmatched", "candidate": None}},
            "watch": {"state": {"watch_marks": []}},
        }}

    async def indexed_counts(_parser, _context, *, dimensions, **_kwargs):
        return {
            "review.state": {
                key: int(key in {"initial_pending", "deep_pending"})
                for key in dimensions["review.state"]
            },
            "qmf.state": {key: 0 for key in dimensions["qmf.state"]},
            "registration.status": {"review_required": 0},
        }

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_task_read_model_group_pages", group_pages)
    monkeypatch.setattr(mobile_tasks, "_read_task_view_states_for_records", view_states)
    monkeypatch.setattr(mobile_tasks, "_indexed_task_state_counts", indexed_counts)

    result = await mobile_tasks._list_analysis_tasks_data_from_read_model(
        AnalysisTaskSearch(
            parser_types=[parser_type], scope="all", review_stage="initial_pending",
            page=1, page_size=1,
        ),
        {"id": 1, "permissions": []},
        _Connection([]),
    )

    assert result["total"] == 1
    assert len(result["data"]) == 1
    assert result["data"][0]["row_key"] == "analysis-a"
    assert result["facets"]["total"] == 2
    assert result["facets"]["review_stage_counts"]["initial_pending"] == 1
    assert result["facets"]["review_stage_counts"]["deep_pending"] == 1
    assert requested_state_filters == [None, {"review.state": ["initial_pending"]}]


@pytest.mark.asyncio
async def test_assignment_selection_uses_redis_eligibility_and_ignores_address_match(
    read_model, monkeypatch,
):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    base = {parser.COMMUNITY_COLUMN: "社区甲", "地址": "合成地址"}
    records = [
        _task_record(parser_type, 117, "eligible", {**base, "核查结果": ""}),
        _task_record(parser_type, 118, "already-assigned", {**base, "核查人": "民警甲"}),
        _task_record(parser_type, 119, "already-complete", {**base, "核查结果": "在吴"}),
        _task_record(parser_type, 120, "multiple-sources", {**base, "核查结果": ""}, source_count=2),
    ]
    # Address matching is an independent domain and must not gate assignment.
    records[0]["address_match_status"] = "conflict"
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    async def assignment_context(*_args, **_kwargs):
        return {
            "community_aliases": {"社区甲": "正式社区甲"},
            "inspectors_by_community": {"正式社区甲": ["组员甲"]},
        }

    requested_state_filters = {}

    async def assignment_group_pages(*args, **kwargs):
        requested_state_filters.update(kwargs.get("state_filters") or {})
        async for page in _assignment_group_pages(
            [_assignment_group(record) for record in records], page_size=2,
        )(*args, **kwargs):
            yield page

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(
        mobile_tasks, "_iter_assignment_read_model_groups", assignment_group_pages,
    )
    states = _install_task_view_states(monkeypatch, records)
    states[records[0]["task_id"]]["address_match"]["state"]["candidate"] = {
        "name": "小区甲",
    }
    states[records[0]["task_id"]]["watch"]["state"]["watch_marks"] = [{
        "category_id": 7,
    }]
    monkeypatch.setattr(mobile_tasks, "inspector_option_context", assignment_context)
    monkeypatch.setattr(mobile_tasks, "_require_task_edit_user", lambda value: value)
    monkeypatch.setattr(mobile_tasks, "_can_assign_tasks", lambda _context: True)
    conn = _Connection([])

    result = await mobile_tasks.select_mobile_tasks_for_assignment(
        parser_type=parser_type,
        data=TaskSearch(
            scope="all", status="pending", small_communities=["小区甲"],
            match_status=["conflict"], watch_categories=[7],
            qmf_feedback_states=["not_scanned"],
        ),
        user={"id": 1, "permissions": [ONLINE_RAW_EDIT]},
        conn=conn,
    )

    assert result == {"row_keys": ["eligible"], "total": 1, "community": "正式社区甲"}
    assert requested_state_filters == {
        "address_match.small_community_name": ["小区甲"],
        "address_match.status": ["conflict"],
        "watch.category_id": ["7"],
        "qmf.state": ["not_scanned"],
    }
    assert all("_online_source_projection" not in sql for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_assignment_selection_stops_after_max_plus_one_eligible_groups(
    read_model, monkeypatch,
):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    records = [
        _task_record(
            parser_type, index + 1, f"row-{index}",
            {parser.COMMUNITY_COLUMN: "社区甲", "核查结果": ""},
        )
        for index in range(mobile_tasks.MAX_BULK_ASSIGNMENT_TASKS + 1)
    ]
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }
    pages_seen = []

    async def flow_context(_conn, _user):
        return context

    async def group_pages(*_args, **_kwargs):
        for offset in range(0, len(records), 400):
            pages_seen.append(offset)
            yield [_assignment_group(record) for record in records[offset:offset + 400]]

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(mobile_tasks, "_iter_assignment_read_model_groups", group_pages)
    _install_task_view_states(monkeypatch, records)
    monkeypatch.setattr(mobile_tasks, "_require_task_edit_user", lambda value: value)
    monkeypatch.setattr(mobile_tasks, "_can_assign_tasks", lambda _context: True)

    with pytest.raises(HTTPException) as error:
        await mobile_tasks.select_mobile_tasks_for_assignment(
            parser_type=parser_type,
            data=TaskSearch(scope="all", status="pending"),
            user={"id": 1, "permissions": [ONLINE_RAW_EDIT]},
            conn=_Connection([]),
        )

    assert error.value.status_code == 409
    assert pages_seen == [0, 400, 800, 1200, 1600, 2000]


@pytest.mark.asyncio
async def test_assignment_selection_rejects_all_scope_for_non_admin(read_model, monkeypatch):
    async def flow_context(_conn, _user):
        return {
            "admin_mode": False, "community_values": ["社区甲"], "name": "组长甲",
            "position": "组长", "community": "社区甲",
        }

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    conn = _Connection([])

    with pytest.raises(HTTPException) as error:
        await mobile_tasks.select_mobile_tasks_for_assignment(
            parser_type="疑似未注销模型三",
            data=TaskSearch(scope="all", status="pending"),
            user={"id": 1, "permissions": [ONLINE_RAW_EDIT]},
            conn=conn,
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_my_history_remains_available_when_task_read_model_is_enabled(
    read_model, monkeypatch,
):
    from datetime import datetime

    history_row = (
        41, "mobile_tasks.task_update", "mobile_task", "全链条:synthetic-row",
        "success", datetime(2026, 11, 5, 1, 2, 3),
    )
    conn = _Connection([history_row])

    result = await mobile_tasks.my_task_history(
        limit=10, user={"id": 7}, conn=conn,
    )

    assert result["data"] == [{
        "id": 41,
        "action": "mobile_tasks.task_update",
        "target_type": "mobile_task",
        "task_key": "全链条:synthetic-row",
        "result": "success",
        "created_at": "2026-11-05T01:02:03Z",
    }]
    assert all("_online_source_projection" not in sql
               for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_assignment_workbench_streams_groups_and_uses_stable_group_order(
    read_model, monkeypatch,
):
    parser_type = "疑似未注销模型三"
    parser = mobile_tasks.get_parser(parser_type)
    base = {parser.COMMUNITY_COLUMN: "社区甲"}
    records = [
        _task_record(parser_type, 117, "row-b", {**base, "地址": "B路 2号"}),
        _task_record(parser_type, 118, "row-a", {**base, "地址": "A路1号"}),
        _task_record(
            parser_type, 119, "already-assigned",
            {**base, "地址": "C路1号", "核查人": "民警甲"},
        ),
        _task_record(
            parser_type, 120, "already-complete",
            {**base, "地址": "D路1号", "核查结果": "在吴"},
        ),
        _task_record(
            parser_type, 121, "multiple-sources",
            {**base, "地址": "E路1号"}, source_count=2,
        ),
    ]
    context = {
        "admin_mode": True, "community_values": None, "name": "管理员",
        "position": "管理员", "community": "全所",
    }

    async def flow_context(_conn, _user):
        return context

    async def assignment_context(*_args, **_kwargs):
        return {
            "community_aliases": {"社区甲": "正式社区甲"},
            "inspectors_by_community": {"正式社区甲": ["组员甲"]},
        }

    monkeypatch.setattr(mobile_tasks, "_flow_context", flow_context)
    monkeypatch.setattr(
        mobile_tasks, "_iter_assignment_read_model_groups",
        _assignment_group_pages([_assignment_group(record) for record in records], page_size=2),
    )
    _install_task_view_states(monkeypatch, records)
    monkeypatch.setattr(mobile_tasks, "inspector_option_context", assignment_context)
    monkeypatch.setattr(mobile_tasks, "_require_task_edit_user", lambda value: value)
    monkeypatch.setattr(mobile_tasks, "_can_assign_tasks", lambda _context: True)
    conn = _Connection([])

    result = await mobile_tasks.get_mobile_task_assignment_workbench(
        parser_type=parser_type,
        user={"id": 1, "permissions": [ONLINE_RAW_EDIT]},
        conn=conn,
    )

    assert [item["row_key"] for item in result["data"]] == ["row-b", "row-a"]
    assert [item["address"] for item in result["data"]] == ["B路 2号", "A路1号"]
    assert result["total"] == result["displayed_total"] == 2
    assert result["communities"] == [{
        "value": "正式社区甲", "label": "正式社区甲", "count": 2,
        "assigned_count": 1,
    }]
    assert result["query_mode"] == "redis_read_model"
    assert all("_online_source_projection" not in sql
               for cursor in conn.cursors for sql in cursor.statements)


@pytest.mark.asyncio
async def test_save_context_reads_task_facts_from_exact_redis_snapshots(
    monkeypatch,
):
    monkeypatch.setattr(mobile_tasks.settings, "TASK_READ_MODEL_ENABLED", True)
    parser_type = "全链条"
    source_rows = [(117, 17, 4), (118, 18, 2)]
    snapshots = {
        117: {"社区": "社区甲", "姓名": "合成对象", "核查结果": "待登记", "核查人": ""},
        118: {"社区": "社区甲", "姓名": "合成对象", "核查结果": "待登记", "核查人": ""},
    }

    async def read_snapshots(current_type, rows):
        assert current_type == parser_type
        assert rows == source_rows
        return snapshots

    registration_link = {"status": "none"}
    review_flow = {"state": "initial_pending", "events": []}
    address_match = {
        "status": "unmatched", "candidate": None, "candidates": [],
        "small_community_id": None, "small_community_name": "",
    }
    monkeypatch.setattr(
        mobile_tasks, "_read_task_snapshots_for_source_rows", read_snapshots,
    )
    parser = mobile_tasks.get_parser(parser_type)
    task_id = f"{parser.table_name}:17"
    monkeypatch.setattr(
        mobile_tasks, "_read_task_view_states_for_records",
        AsyncMock(return_value={task_id: {
            "address_match": {"state": address_match},
            "watch": {"state": {"status": "none", "watch_marks": []}},
            "qmf": {"state": {"state": "not_scanned"}},
            "residence": {"state": {"state": "pending"}},
            "registration": {"state": registration_link},
            "review": {"state": review_flow},
        }}),
    )
    conn = _Connection(source_rows)

    result = await mobile_tasks._mobile_task_save_context(
        conn, parser_type, "synthetic-row",
    )

    task_update = result["task_update"]
    assert task_update["row_key"] == "synthetic-row"
    assert task_update["community"] == "社区甲"
    assert task_update["summary"]["title"] == "合成对象"
    assert task_update["source_count"] == 2
    assert task_update["conflict"] is False
    assert task_update["state"] == mobile_tasks.TASK_WORKFLOWS[parser_type].state(snapshots[117])
    assert task_update["review_flow"] == review_flow
    assert task_update["address_match"] == address_match
    assert result["registration_link"] == registration_link
    sql = " ".join(statement for cursor in conn.cursors for statement in cursor.statements)
    assert "_online_source_rows" in sql
    assert "_online_source_projection" not in sql
    assert "_task_registration_links" not in sql
    assert "_unverifiable_review_flows" not in sql
    assert "_online_task_address_matches" not in sql


@pytest.mark.asyncio
async def test_save_context_marks_conflict_from_redis_source_snapshots(monkeypatch):
    monkeypatch.setattr(mobile_tasks.settings, "TASK_READ_MODEL_ENABLED", True)
    parser_type = "全链条"
    source_rows = [(117, 17, 4), (118, 18, 2)]
    snapshots = {
        117: {"社区": "社区甲", "姓名": "合成对象甲", "核查结果": "待登记"},
        118: {"社区": "社区甲", "姓名": "合成对象乙", "核查结果": "待登记"},
    }

    async def read_snapshots(_parser_type, _rows):
        return snapshots

    monkeypatch.setattr(
        mobile_tasks, "_read_task_snapshots_for_source_rows", read_snapshots,
    )
    parser = mobile_tasks.get_parser(parser_type)
    monkeypatch.setattr(
        mobile_tasks, "_read_task_view_states_for_records",
        AsyncMock(return_value={f"{parser.table_name}:17": {
            "address_match": {"state": {
                "status": "unmatched", "candidate": None, "candidates": [],
            }},
            "watch": {"state": {"status": "none", "watch_marks": []}},
            "qmf": {"state": {"state": "not_scanned"}},
            "residence": {"state": {"state": "pending"}},
            "registration": {"state": {"status": "none"}},
            "review": {"state": {"state": "not_applicable", "events": []}},
        }}),
    )

    result = await mobile_tasks._mobile_task_save_context(
        _Connection(source_rows), parser_type, "synthetic-row",
    )

    assert result["task_update"]["source_count"] == 2
    assert result["task_update"]["conflict"] is True
