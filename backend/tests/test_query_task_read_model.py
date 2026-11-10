import json
import os
import sys
import types

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault(
    "TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime",
)

import pytest
from fastapi import HTTPException

from routers import query
from services import task_read_model
from services.parsers import get_parser


class _Cursor:
    def __init__(self, revision_rows=None):
        self.rows = []
        self.revision_rows = revision_rows or [(17, 1)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, _params=None):
        if "SELECT id,revision" in sql:
            self.rows = [
                (*row, row[0], "stable-row-key") if len(row) == 2 else row
                for row in self.revision_rows
            ]
        elif "SELECT cell_meta_json" in sql:
            self.rows = [("{}",)]
        else:
            self.rows = []

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, revision_rows=None):
        self.revision_rows = revision_rows

    def cursor(self):
        return _Cursor(self.revision_rows)


class _Redis:
    async def ping(self):
        return True

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_management_query_pushes_allowlisted_filters_and_community_scope_to_redis(
    monkeypatch,
):
    parser = get_parser("全链条")
    def make_row(source_id, name, result):
        values = {column: "" for column in parser.COLUMNS}
        values.update({
            "社区": "社区甲", "姓名": name, "核查结果": result,
            "身份证号": str(source_id), "电话号码": "synthetic-phone",
            "下发日期": "2026-11-04",
        })
        return {
            "task_id": f"{parser.table_name}:{source_id}",
            "source_id": source_id,
            "revision": 1,
            "event_id": f"event-{source_id}",
            "updated_at": "2026-11-04T00:00:00Z",
            "values": values,
        }

    rows_by_id = {
        row["task_id"]: row
        for row in (
            make_row(17, "合成乙", "待核查"),
            make_row(18, "来源冲突", "待核查"),
            make_row(19, "合成甲", "待核查"),
        )
    }
    rows_by_id[f"{parser.table_name}:18"]["values"]["身份证号"] = "17"
    captured = {}

    monkeypatch.setattr(query.settings, "APP_ENVIRONMENT", "development")
    monkeypatch.setattr(query.settings, "TASK_READ_MODEL_RUN_ID", "dev-query-test")
    monkeypatch.setattr(
        query.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 32,
    )
    monkeypatch.setattr(query.settings, "REDIS_URL", "redis://unused")
    monkeypatch.setattr(query, "effective_view_communities", lambda _user: ["dept-1"])

    async def community_names(_conn, _scopes):
        return ["社区甲"]

    async def query_group_ids(
        _client, _environment, _run_id, parser_type, filters,
        communities, search, offset, limit,
    ):
        captured.update({
            "parser_type": parser_type,
            "filters": filters, "communities": communities, "search": search,
            "offset": offset, "limit": limit,
        })
        return {"total": 2, "group_tokens": ["a" * 64, "b" * 64][offset:offset + limit]}

    async def read_group_records(
        _client, _environment, _run_id, _parser_type, group_tokens,
    ):
        captured["group_tokens"] = group_tokens
        return [{
            "parser_type": parser.table_name,
            "row_key": parser.make_row_key(rows_by_id[f"{parser.table_name}:17"]["values"]),
            "task_id": f"{parser.table_name}:17",
            "source_id": 17,
            "revision": 1,
            "updated_at": "2026-11-04T00:00:00Z",
            "values": rows_by_id[f"{parser.table_name}:17"]["values"],
            "sources": [
                {
                    "task_id": rows_by_id[f"{parser.table_name}:{source_id}"]["task_id"],
                    "source_id": source_id,
                    "revision": 1,
                    "values": rows_by_id[f"{parser.table_name}:{source_id}"]["values"],
                }
                for source_id in (17, 18)
            ],
            "source_count": 2,
            "conflict": True,
        }]

    async def ready(*_args):
        return None

    async def watermark(*_args):
        return 9

    async def inspector_options(*_args):
        return {}

    async def column_metadata(_cur, current_parser, *_args, **_kwargs):
        return {column: {} for column in current_parser.COLUMNS}

    async def edit_capabilities(*_args):
        return {"editable_fields": []}

    monkeypatch.setattr(query, "community_names_for_scopes", community_names)
    monkeypatch.setattr(query, "inspector_option_context", inspector_options)
    monkeypatch.setattr(query, "_managed_column_metadata", column_metadata)
    monkeypatch.setattr(query, "_query_row_edit_capabilities", edit_capabilities)
    monkeypatch.setattr(query, "can_manage_rows", lambda _user: False)
    monkeypatch.setattr(query, "local_row_hash", lambda _values: "row-hash")
    monkeypatch.setattr(task_read_model, "query_task_read_model_group_ids", query_group_ids)
    monkeypatch.setattr(task_read_model, "read_task_read_model_group_records", read_group_records)
    monkeypatch.setattr(task_read_model, "require_task_read_model_ready", ready)
    monkeypatch.setattr(task_read_model, "task_read_model_watermark", watermark)

    redis_package = types.ModuleType("redis")
    redis_package.__path__ = []
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.from_url = lambda *_args, **_kwargs: _Redis()
    monkeypatch.setitem(sys.modules, "redis", redis_package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)

    result = await query._task_read_model_query(
        parser_type="全链条",
        page=1,
        page_size=1,
        keyword=None,
        sort_by="姓名",
        sort_order="asc",
        filters=json.dumps({"核查结果": ["待核查"]}, ensure_ascii=False),
        grid_filters=None,
        user={"id": 8},
            conn=_Connection([
                (17, 1, 17, parser.make_row_key(rows_by_id[f"{parser.table_name}:17"]["values"])),
                (18, 1, 18, parser.make_row_key(rows_by_id[f"{parser.table_name}:17"]["values"])),
                (19, 1, 19, parser.make_row_key(rows_by_id[f"{parser.table_name}:19"]["values"])),
            ]),
    )

    assert captured["parser_type"] == parser.table_name
    assert captured["communities"] == ["社区甲"]
    assert captured["filters"] == {"核查结果": ["待核查"]}
    assert captured["search"] is None
    assert captured["offset"] == 0
    assert captured["limit"] == 1
    assert captured["group_tokens"] == ["a" * 64]
    assert result["total"] == 2
    assert result["data"][0]["姓名"] == "合成乙"
    assert result["data"][0]["__source_count"] == 2
    assert result["data"][0]["__conflict"] is True
    assert result["data"][0]["__source_id"] == 17


@pytest.mark.asyncio
async def test_management_query_rejects_unindexable_short_search(monkeypatch):
    monkeypatch.setattr(query.settings, "APP_ENVIRONMENT", "development")
    monkeypatch.setattr(query.settings, "TASK_READ_MODEL_RUN_ID", "dev-query-test")
    monkeypatch.setattr(query.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setattr(query, "effective_view_communities", lambda _user: None)

    with pytest.raises(HTTPException) as error:
        await query._task_read_model_query(
            parser_type="全链条",
            page=1,
            page_size=20,
            keyword="a",
            sort_by=None,
            sort_order="desc",
            filters=None,
            grid_filters={"姓名": {"type": "contains", "filter": "合成"}},
            user={"id": 8},
            conn=_Connection(),
        )

    assert error.value.status_code == 422
    assert error.value.detail == {"error": "invalid_projection_search"}


@pytest.mark.asyncio
async def test_grid_scan_checks_complete_cross_community_group_before_returning(
    monkeypatch,
):
    parser = get_parser("全链条")
    canonical_values = {column: "" for column in parser.COLUMNS}
    canonical_values.update({
        "社区": "社区甲", "姓名": "合成姓名", "核查结果": "待核查",
        "身份证号": "synthetic-id", "电话号码": "synthetic-phone",
        "下发日期": "2026-11-04",
    })
    other_values = dict(canonical_values)
    other_values["社区"] = "社区乙"
    rows_by_id = {
        f"{parser.table_name}:17": {
            "task_id": f"{parser.table_name}:17", "source_id": 17,
            "revision": 1, "updated_at": "2026-11-04T00:00:00Z",
            "values": canonical_values,
        },
        f"{parser.table_name}:18": {
            "task_id": f"{parser.table_name}:18", "source_id": 18,
            "revision": 1, "updated_at": "2026-11-04T00:00:00Z",
            "values": other_values,
        },
    }
    captured = {}

    monkeypatch.setattr(query.settings, "APP_ENVIRONMENT", "development")
    monkeypatch.setattr(query.settings, "TASK_READ_MODEL_RUN_ID", "dev-query-test")
    monkeypatch.setattr(query.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setattr(query.settings, "REDIS_URL", "redis://unused")
    monkeypatch.setattr(query, "effective_view_communities", lambda _user: ["dept-1"])

    async def community_names(_conn, _scopes):
        return ["社区甲"]

    async def create_scan(_client, **kwargs):
        captured.update(kwargs)
        return {"scan_id": "c" * 32, "total": 2}

    async def read_scan(_client, **kwargs):
        return {
            "records": [
                {"task_id": task_id, "indexed_revision": 1}
                for task_id in rows_by_id
            ],
            "next_cursor": None,
            "total": 2,
        }

    async def read_scan_records(_client, **kwargs):
        return [rows_by_id[item["task_id"]] for item in kwargs["records"]]

    async def close_scan(*_args, **_kwargs):
        return None

    async def ready(*_args):
        return None

    async def watermark(*_args):
        return 9

    async def inspector_options(*_args):
        return {}

    async def column_metadata(_cur, current_parser, *_args, **_kwargs):
        return {column: {} for column in current_parser.COLUMNS}

    async def edit_capabilities(*_args):
        return {"editable_fields": []}

    monkeypatch.setattr(query, "community_names_for_scopes", community_names)
    monkeypatch.setattr(query, "inspector_option_context", inspector_options)
    monkeypatch.setattr(query, "_managed_column_metadata", column_metadata)
    monkeypatch.setattr(query, "_query_row_edit_capabilities", edit_capabilities)
    monkeypatch.setattr(query, "can_manage_rows", lambda _user: False)
    monkeypatch.setattr(query, "local_row_hash", lambda _values: "row-hash")
    monkeypatch.setattr(task_read_model, "create_task_read_model_scan", create_scan)
    monkeypatch.setattr(task_read_model, "read_task_read_model_scan", read_scan)
    monkeypatch.setattr(task_read_model, "read_task_read_model_scan_records", read_scan_records)
    monkeypatch.setattr(task_read_model, "close_task_read_model_scan", close_scan)
    monkeypatch.setattr(task_read_model, "require_task_read_model_ready", ready)
    monkeypatch.setattr(task_read_model, "task_read_model_watermark", watermark)

    redis_package = types.ModuleType("redis")
    redis_package.__path__ = []
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.from_url = lambda *_args, **_kwargs: _Redis()
    monkeypatch.setitem(sys.modules, "redis", redis_package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)

    result = await query._task_read_model_query(
        parser_type="全链条", page=1, page_size=20, keyword=None,
        sort_by=None, sort_order="asc", filters=None,
        grid_filters=json.dumps({"姓名": {"type": "contains", "filter": "合成"}}, ensure_ascii=False),
        user={"id": 8}, conn=_Connection(),
    )

    assert captured["communities"] is None
    assert captured["filters"] == {}
    assert captured["search"] is None
    assert result["total"] == 0
    assert result["data"] == []


@pytest.mark.asyncio
async def test_management_query_checks_revision_of_every_source_in_conflict_group(
    monkeypatch,
):
    parser = get_parser("全链条")
    values = {column: "" for column in parser.COLUMNS}
    values.update({"社区": "社区甲", "姓名": "合成姓名", "核查结果": "待核查"})
    row = {
        "task_id": f"{parser.table_name}:17",
        "source_id": 17,
        "revision": 1,
        "event_id": "event-17",
        "updated_at": "2026-11-04T00:00:00Z",
        "values": values,
    }
    second = {**row, "task_id": f"{parser.table_name}:18", "source_id": 18}

    monkeypatch.setattr(query.settings, "APP_ENVIRONMENT", "development")
    monkeypatch.setattr(query.settings, "TASK_READ_MODEL_RUN_ID", "dev-query-test")
    monkeypatch.setattr(query.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setattr(query.settings, "REDIS_URL", "redis://unused")
    monkeypatch.setattr(query, "effective_view_communities", lambda _user: None)

    async def create_scan(*_args, **_kwargs):
        return {"scan_id": "b" * 32, "total": 2, "cursor": 0}

    async def read_scan(*_args, **_kwargs):
        return {
            "records": [
                {"task_id": row["task_id"], "indexed_revision": 1},
                {"task_id": second["task_id"], "indexed_revision": 1},
            ],
            "next_cursor": None,
            "total": 2,
        }

    async def read_scan_records(*_args, **_kwargs):
        return [row, second]

    async def close_scan(*_args, **_kwargs):
        return None

    async def ready(*_args):
        return None

    async def watermark(*_args):
        return 9

    monkeypatch.setattr(task_read_model, "create_task_read_model_scan", create_scan)
    monkeypatch.setattr(task_read_model, "read_task_read_model_scan", read_scan)
    monkeypatch.setattr(task_read_model, "read_task_read_model_scan_records", read_scan_records)
    monkeypatch.setattr(task_read_model, "close_task_read_model_scan", close_scan)
    monkeypatch.setattr(task_read_model, "require_task_read_model_ready", ready)
    monkeypatch.setattr(task_read_model, "task_read_model_watermark", watermark)
    redis_package = types.ModuleType("redis")
    redis_package.__path__ = []
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.from_url = lambda *_args, **_kwargs: _Redis()
    monkeypatch.setitem(sys.modules, "redis", redis_package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)

    with pytest.raises(HTTPException) as error:
        await query._task_read_model_query(
            parser_type="全链条",
            page=1,
            page_size=20,
            keyword=None,
            sort_by=None,
            sort_order="desc",
            filters=None,
            grid_filters={"姓名": {"type": "contains", "filter": "合成"}},
            user={"id": 8},
            conn=_Connection([
                (17, 1, 17, parser.make_row_key(values)),
                (18, 2, 18, parser.make_row_key(values)),
            ]),
        )

    assert error.value.status_code == 503
    assert error.value.detail == {"error": "projection_pending"}
