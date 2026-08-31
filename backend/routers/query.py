"""在线数据查询、腾讯来源行定位与安全回写 API。"""

from __future__ import annotations

import inspect
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
from config import settings
from deps import require_admin_account, require_permission, require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.data_scope import community_names_for_scopes
from services.online_edit_permissions import (
    can_manage_rows,
    effective_view_communities,
    inspector_assignment_mismatch,
    inspector_option_context,
    row_edit_capabilities,
    validate_inspector_assignment,
    validate_new_row_scope,
    validate_row_changes,
)
from services.online_source import (
    acquire_sheet_lock,
    active_source_sql_filter,
    json_value,
    rebuild_projection,
    rebuild_projection_rows,
    release_sheet_lock,
    replace_source_cache,
    resolve_source_columns,
    source_row_hash,
    stable_json,
    update_cached_source_row,
)
from services.local_source import (
    local_data_source_enabled,
    local_sheet_id,
    local_row_hash,
    local_source_migration_status,
    local_source_migration_issues,
)
from services.online_local_writeback import (
    enqueue_local_changes,
    launch_local_change_processing,
    load_local_changes,
    overlay_local_values,
    source_sync_payload,
)
from services.parsers import PARSER_REGISTRY, get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_RAW_VIEW,
)
from services.schema_compat import get_database_column_map, quote_identifier
from services.task_workflow import TASK_WORKFLOWS, canonical_result_options
from services.task_graph import reconcile_online_task_graph
from services.txdocs_client import TxDocsAPIError, TxDocsClient
from services.work_activity import (
    ONLINE_TASK_UPDATE,
    is_actual_online_work,
    record_work_activity,
)
from services.domain_events import enqueue_event
from services.task_assignment_responsibility import (
    capture_first_assignment,
    migrate_responsibility_row_key,
    task_update_is_credited_to,
)


router = APIRouter(
    prefix="/api/query",
    tags=["数据查询"],
    dependencies=[Depends(require_admin_account)],
)
QUERY_TYPES = [item for item in PARSER_REGISTRY if item != "default"]


async def _connection_transaction_call(conn, method_name: str) -> None:
    """Call an aiomysql transaction method while supporting lightweight test doubles."""
    method = getattr(conn, method_name, None)
    if method is None:
        return
    result = method()
    if inspect.isawaitable(result):
        await result


class CellUpdate(BaseModel):
    column: str = Field(min_length=1, max_length=200)
    value: str = Field(default="", max_length=10000)
    expected_revision: int = Field(gt=0)
    explicit_text_edit: bool = False


class SourceRowCreate(BaseModel):
    values: dict[str, str]


def _json_path(column: str) -> str:
    return '$."' + column.replace('"', '\\"') + '"'


def new_row_required_fields(parser) -> list[str]:
    """返回行内新增提交前必须填写的字段，顺序与腾讯表一致。"""
    required = set(parser.get_business_key())
    if parser.COMMUNITY_COLUMN in parser.COLUMNS:
        required.add(parser.COMMUNITY_COLUMN)
    return [column for column in parser.COLUMNS if column in required]


def _same_value(left: str, right: str, cell_type: str) -> bool:
    if cell_type == "number":
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return str(left or "").strip() == str(right or "").strip()


def _physical_cell_type(metadata: dict | None) -> str:
    """返回腾讯真实物理类型，不能被前端编辑器类型覆盖。"""
    if not isinstance(metadata, dict):
        return "text"
    return str(metadata.get("write_type") or metadata.get("type") or "text")


def _looks_like_automatic_text_coercion(
    column: str,
    before: str,
    after: str,
    physical_type: str,
) -> bool:
    """Detect the known spreadsheet number-coercion corruption pattern.

    This is intentionally conservative and only applies to physical text cells.
    A caller that explicitly typed the replacement text can opt in through the
    ``explicit_text_edit`` flag; the normal Univer auto-reconcile path does not.
    """
    if physical_type not in {"text", "string"} or not before or before == after:
        return False
    if re.search(r"(身份证|证件|手机号|手机|电话)", str(column)):
        if (
            re.fullmatch(r"\d{15,30}[xX]?", before)
            and re.fullmatch(r"\d{12,32}", after)
            and before[:12] == after[:12]
        ):
            return True
    if re.fullmatch(r"-?\d+\.\d*0", before) and re.fullmatch(r"-?\d+(?:\.\d+)?", after):
        try:
            return Decimal(before) == Decimal(after)
        except (InvalidOperation, ValueError):
            return False
    return False


def _editor_select_metadata(
    source: dict,
    options: list[dict],
) -> dict:
    """选择器元数据与腾讯物理写入元数据分别保存。"""
    write_type = _physical_cell_type(source)
    write_options = list(source.get("write_options") or source.get("options") or [])
    write_multiple = bool(
        source.get("write_multiple", source.get("multiple", False))
    )
    usable_write_options = (
        _usable_select_options({"options": write_options})
        if write_type == "select"
        else []
    )
    editor_options = usable_write_options or options
    # Some Tencent responses expose the cell as a select but omit its option
    # definitions (this is especially common for blank cells).  The caller
    # supplies a cached or workflow fallback list in that case.  Keep the same
    # list for physical writeback; otherwise the editor displays a valid value
    # while build_update_cell_request rejects it as an unknown option.
    if write_type == "select" and not usable_write_options:
        write_options = list(options)
    return {
        "type": "select",
        "multiple": False,
        "options": editor_options,
        "write_type": write_type,
        "write_multiple": write_multiple,
        "write_options": write_options,
    }


def _usable_select_options(metadata: dict | None) -> list[dict]:
    """过滤腾讯空白单元格偶尔返回的空下拉选项。"""
    if not isinstance(metadata, dict):
        return []
    result = []
    for option in metadata.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "").strip()
        text = str(option.get("text") or "").strip()
        if not option_id or not text:
            continue
        normalized = dict(option)
        normalized["id"] = option_id
        normalized["text"] = text
        result.append(normalized)
    return result


async def _cached_result_options(
    cur,
    parser_type: str,
    field: str,
    *,
    spreadsheet_id: int | None = None,
    sheet_id: str | None = None,
) -> list[dict]:
    """从同业务已缓存的非空单元格复用腾讯原始选项 ID。"""
    options_path = f'$."{field}".options'
    scope_sql = ""
    params: list[Any] = [parser_type, options_path]
    if spreadsheet_id is not None:
        scope_sql += " AND spreadsheet_id=%s"
        params.append(spreadsheet_id)
    if sheet_id:
        scope_sql += " AND sheet_id=%s"
        params.append(sheet_id)
    await cur.execute(
        f"""
        SELECT cell_meta_json
        FROM _online_source_rows
        WHERE parser_type=%s
          AND archived_at IS NULL
          AND JSON_LENGTH(JSON_EXTRACT(cell_meta_json, %s)) > 1
          {scope_sql}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    )
    for row in await cur.fetchall():
        cached = json_value(row[0], {})
        options = _usable_select_options(cached.get(field))
        if options:
            return options
    return []


async def _managed_column_metadata(
    cur,
    parser,
    source_metadata: dict | None = None,
    *,
    spreadsheet_id: int | None = None,
    sheet_id: str | None = None,
    inspector_context: dict | None = None,
) -> dict[str, dict]:
    """补齐社区、核查人和业务结果的稳定下拉选项。"""
    metadata = {}
    for column in parser.COLUMNS:
        source = dict((source_metadata or {}).get(column) or {"type": "text"})
        source.setdefault("write_type", source.get("type", "text"))
        source.setdefault("write_multiple", bool(source.get("multiple", False)))
        source.setdefault("write_options", list(source.get("options") or []))
        metadata[column] = source
    if parser.COMMUNITY_COLUMN in parser.COLUMNS:
        if inspector_context is not None:
            communities = sorted({
                str(value).strip()
                for value in (inspector_context.get("community_aliases") or {}).values()
                if str(value).strip()
            })
        else:
            await cur.execute(
                """
                SELECT community.name
                FROM _communities AS community
                JOIN _departments AS department
                  ON department.community_id=community.id
                 AND department.department_type='community'
                 AND department.is_active=1
                WHERE community.is_active=1
                ORDER BY community.id
                """
            )
            communities = [str(row[0]) for row in await cur.fetchall() if row[0]]
        metadata[parser.COMMUNITY_COLUMN] = _editor_select_metadata(
            metadata[parser.COMMUNITY_COLUMN],
            [{"id": name, "text": name} for name in communities],
        )
    if "核查人" in parser.COLUMNS:
        members = list((inspector_context or {}).get("fallback_inspectors") or [])
        if not inspector_context:
            await cur.execute(
                """
                SELECT DISTINCT member.name
                FROM _grid_members AS member
                JOIN _grid_member_department_links AS link
                  ON link.member_id=member.id
                JOIN _departments AS department
                  ON department.id=link.department_id
                 AND department.department_type='community'
                 AND department.is_active=1
                WHERE member.position IN ('组长', '组员')
                  AND member.status='在岗'
                ORDER BY member.name
                """
            )
            members = [str(row[0]) for row in await cur.fetchall() if row[0]]
        metadata["核查人"] = _editor_select_metadata(
            metadata["核查人"],
            [{"id": name, "text": name} for name in members],
        )
        # 真实腾讯下拉选项保存在 write_options；编辑器只展示当前账号的
        # 安全兜底名单，逐行名单由响应中的 dependent_options 提供。
        metadata["核查人"]["options"] = [
            {"id": name, "text": name} for name in members
        ]
    workflow = TASK_WORKFLOWS.get(parser.parser_type)
    if workflow and workflow.result_field in parser.COLUMNS:
        result_field = workflow.result_field
        options = _usable_select_options(metadata.get(result_field))
        if not options:
            options = await _cached_result_options(
                cur,
                parser.parser_type,
                result_field,
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
            )
        options = canonical_result_options(parser.parser_type, options)
        if not options:
            options = [
                {"id": text, "text": text}
                for text in workflow.result_options
            ]
        else:
            known_texts = {
                str(option.get("text") or "").strip()
                for option in options
            }
            options.extend(
                {"id": text, "text": text}
                for text in workflow.result_options
                if text not in known_texts
            )
        metadata[result_field] = _editor_select_metadata(
            metadata[result_field], options
        )
        # 业务结果允许在保留腾讯原有选项 ID 的同时补充平台新增选项。
        # 必须同时更新编辑器和写回校验列表，否则页面虽然能展示
        # 新选项，保存时仍会被判定为“无效的下拉选项”。
        metadata[result_field]["options"] = list(options)
        if metadata[result_field].get("write_type") == "select":
            metadata[result_field]["write_options"] = list(options)
    return metadata


def _row_values_match(
    expected: dict[str, str],
    actual: dict[str, str],
    actual_metadata: dict[str, dict],
    columns: list[str],
) -> bool:
    return all(
        _same_value(
            actual.get(column, ""),
            expected.get(column, ""),
            _physical_cell_type(actual_metadata.get(column)),
        )
        for column in columns
    )


def _grid_filter_condition(
    expression: str,
    model: dict,
) -> tuple[str, list[Any]] | None:
    if not isinstance(model, dict):
        return None
    filter_type = str(model.get("type") or "contains")
    value = str(model.get("filter") or "")
    if filter_type == "blank":
        return f"COALESCE({expression}, '') = ''", []
    if filter_type == "notBlank":
        return f"COALESCE({expression}, '') <> ''", []
    if filter_type == "equals":
        return f"{expression} = %s", [value]
    if filter_type == "notEqual":
        return f"{expression} <> %s", [value]
    if filter_type == "startsWith":
        return f"{expression} LIKE %s", [f"{value}%"]
    if filter_type == "notStartsWith":
        return f"{expression} NOT LIKE %s", [f"{value}%"]
    if filter_type == "endsWith":
        return f"{expression} LIKE %s", [f"%{value}"]
    if filter_type == "notEndsWith":
        return f"{expression} NOT LIKE %s", [f"%{value}"]
    if filter_type == "notContains":
        return f"{expression} NOT LIKE %s", [f"%{value}%"]
    if filter_type in {
        "greaterThan",
        "lessThan",
        "greaterThanOrEqual",
        "lessThanOrEqual",
    }:
        operator = {
            "greaterThan": ">",
            "lessThan": "<",
            "greaterThanOrEqual": ">=",
            "lessThanOrEqual": "<=",
        }[filter_type]
        return f"CAST({expression} AS DECIMAL(30, 6)) {operator} %s", [value]
    return f"{expression} LIKE %s", [f"%{value}%"]


def _append_grid_filters(
    where_parts: list[str],
    params: list[Any],
    raw_filters: str | None,
    columns: list[str],
    expression_for,
) -> None:
    if not raw_filters:
        return
    try:
        filters = json.loads(raw_filters)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(filters, dict):
        return
    for column, model in filters.items():
        if column not in columns or not isinstance(model, dict):
            continue
        expression = expression_for(column)
        raw_conditions = model.get("conditions")
        if isinstance(raw_conditions, list) and raw_conditions:
            conditions = [
                condition
                for item in raw_conditions[:2]
                if (condition := _grid_filter_condition(expression, item)) is not None
            ]
            if not conditions:
                continue
            joiner = " AND " if str(model.get("operator") or "and") == "and" else " OR "
            where_parts.append("(" + joiner.join(sql for sql, _ in conditions) + ")")
            for _, condition_params in conditions:
                params.extend(condition_params)
            continue
        condition = _grid_filter_condition(expression, model)
        if condition is None:
            continue
        sql, condition_params = condition
        where_parts.append(sql)
        params.extend(condition_params)


async def _writeback_enabled(cur) -> bool:
    if local_data_source_enabled():
        return True
    await cur.execute(
        "SELECT config_value FROM _system_config "
        "WHERE config_key='online_writeback_enabled'"
    )
    row = await cur.fetchone()
    return str(row[0] if row else "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def _enabled_spreadsheets(cur, parser_type: str) -> list[dict]:
    await cur.execute(
        """
        SELECT id, name, file_id, data_sheet_id, header_row, parser_type
        FROM _config_spreadsheets
        WHERE parser_type=%s AND enabled=1
          AND file_id<>'' AND data_sheet_id<>''
        ORDER BY id
        """,
        (parser_type,),
    )
    return [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "file_id": str(row[2]),
            "data_sheet_id": str(row[3]),
            "header_row": int(row[4] or 1),
            "parser_type": str(row[5]),
        }
        for row in await cur.fetchall()
    ]


async def _source_ready(cur, spreadsheets: list[dict]) -> bool:
    if not spreadsheets:
        return False
    ids = [item["id"] for item in spreadsheets]
    placeholders = ", ".join(["%s"] * len(ids))
    await cur.execute(
        f"SELECT COUNT(*) FROM _online_source_cache_state "
        f"WHERE spreadsheet_id IN ({placeholders})",
        ids,
    )
    return int((await cur.fetchone())[0] or 0) == len(ids)


async def _source_data_version(cur, parser_type: str) -> str:
    """Return a non-sensitive token that changes when cached source rows change."""
    await cur.execute(
        f"""
        SELECT COUNT(*), COALESCE(SUM(revision), 0), MAX(refreshed_at)
        FROM _online_source_rows AS source
         WHERE source.parser_type=%s
           AND source.archived_at IS NULL
        {active_source_sql_filter(parser_type)}
        """,
        (parser_type,),
    )
    count, revision_sum, refreshed_at = await cur.fetchone()
    timestamp = refreshed_at.isoformat() if refreshed_at else ""
    return f"{int(count or 0)}:{int(revision_sum or 0)}:{timestamp}"


async def _oauth_client(cur) -> TxDocsClient:
    await cur.execute(
        "SELECT client_id, access_token, open_id "
        "FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row[1] or not row[2]:
        raise HTTPException(503, "腾讯文档 OAuth 尚未配置")
    return TxDocsClient(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        usage_source="online_query",
    )


async def _load_source_row(cur, parser_type: str, source_id: int, *, lock: bool = False) -> dict:
    sql = f"""
        SELECT source.id, source.spreadsheet_id, source.sheet_id,
               source.physical_row, source.row_key, source.row_hash,
               source.values_json, source.cell_meta_json, source.revision,
               spreadsheet.name, spreadsheet.file_id,
               spreadsheet.data_sheet_id, spreadsheet.header_row,
               spreadsheet.enabled, source.source_kind, source.source_ref
        FROM _online_source_rows AS source
        LEFT JOIN _config_spreadsheets AS spreadsheet
          ON spreadsheet.id=source.spreadsheet_id
         WHERE source.id=%s AND source.parser_type=%s
           AND source.archived_at IS NULL
           {active_source_sql_filter(parser_type, 'source')}
        """
    if lock:
        sql += " FOR UPDATE"
    await cur.execute(
        sql,
        (source_id, parser_type),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(409, "来源行已经变化，请刷新后重试")
    if int(row[1]) != 0 and not row[13]:
        raise HTTPException(409, "该来源表已经停用")
    return {
        "id": int(row[0]),
        "spreadsheet_id": int(row[1]),
        "sheet_id": str(row[2]),
        "physical_row": int(row[3]),
        "row_key": str(row[4]),
        "row_hash": str(row[5]),
        "values": json_value(row[6], {}),
        "cell_meta": json_value(row[7], {}),
        "revision": int(row[8]),
        "source_kind": str(row[14] or ""),
        "source_ref": str(row[15] or ""),
        "spreadsheet": {
            "id": int(row[1]),
            "name": str(row[9] or "本地业务数据"),
            "file_id": str(row[10] or ""),
            "data_sheet_id": str(row[11] or local_sheet_id(parser_type)),
            "header_row": int(row[12] or 1),
            "parser_type": parser_type,
        },
    }


async def _refresh_spreadsheet(conn, client, spreadsheet: dict) -> None:
    parser = get_parser(spreadsheet["parser_type"])
    source_columns = await resolve_source_columns(client, spreadsheet, parser)
    rows = await client.read_all_source_rows(
        spreadsheet["file_id"],
        spreadsheet["data_sheet_id"],
        spreadsheet["header_row"],
        source_columns,
    )
    await replace_source_cache(conn, spreadsheet, rows)


async def _insert_writeback_audit(
    cur,
    *,
    user: dict,
    action: str,
    parser_type: str,
    spreadsheet_id: int,
    sheet_id: str,
    physical_row: int | None,
    column_name: str | None,
    row_key_before: str | None,
    row_key_after: str | None,
    before_values: dict | None,
    after_values: dict | None,
    sync_status: str = "pending",
) -> int:
    await cur.execute(
        """
        INSERT INTO _online_writeback_audit (
            user_id, username, action, parser_type, spreadsheet_id,
            sheet_id, physical_row, column_name, row_key_before,
            row_key_after, before_values, after_values, sync_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user["id"],
            str(user.get("username") or "")[:50],
            action,
            parser_type,
            spreadsheet_id,
            sheet_id,
            physical_row,
            column_name,
            row_key_before,
            row_key_after,
            stable_json(before_values) if before_values is not None else None,
            stable_json(after_values) if after_values is not None else None,
            sync_status,
        ),
    )
    return int(cur.lastrowid)


async def _update_writeback_audit(
    cur,
    audit_id: int,
    status: str,
    *,
    row_key_after: str | None = None,
    after_values: dict | None = None,
) -> None:
    if after_values is None:
        await cur.execute(
            "UPDATE _online_writeback_audit SET sync_status=%s WHERE id=%s",
            (status, audit_id),
        )
        return
    await cur.execute(
        "UPDATE _online_writeback_audit "
        "SET sync_status=%s, row_key_after=%s, after_values=%s WHERE id=%s",
        (status, row_key_after, stable_json(after_values), audit_id),
    )


async def _update_local_source_fields(
    *,
    parser_type: str,
    source_id: int,
    changes: dict[str, str],
    expected_revision: int,
    request: Request,
    user: dict,
    conn,
    explicit_text_edit: bool = False,
    allowed_columns: set[str] | None = None,
    current_values_validator=None,
    redact_audit_values: bool = False,
    system_managed_columns: set[str] | None = None,
    base_values: dict[str, str] | None = None,
    registration_mode: bool = False,
    audit_action: str = "local_update",
    transaction_prepare=None,
    transaction_callback=None,
    record_unverifiable_save: bool = True,
) -> dict:
    """本地数据源的事务更新路径，不访问腾讯文档。"""
    parser = get_parser(parser_type)
    normalized_changes = {
        str(column): str(value or "").strip()
        for column, value in changes.items()
    }
    if not normalized_changes:
        raise HTTPException(400, "没有需要保存的修改")
    if len(normalized_changes) > 5:
        raise HTTPException(400, "一次最多保存 5 个字段")
    unknown = [column for column in normalized_changes if column not in parser.COLUMNS]
    if unknown:
        raise HTTPException(400, f"字段不存在：{'、'.join(unknown)}")
    if allowed_columns is not None and any(column not in allowed_columns for column in normalized_changes):
        raise HTTPException(400, "提交包含当前入口不允许修改的字段")
    if system_managed_columns is not None and (
        allowed_columns != system_managed_columns
        or set(normalized_changes) != system_managed_columns
    ):
        raise RuntimeError("system-managed edit must use an exact field whitelist")
    ordered_columns = [column for column in parser.COLUMNS if column in normalized_changes]

    await _connection_transaction_call(conn, "begin")
    async with conn.cursor() as cur:
        source = await _load_source_row(cur, parser_type, source_id, lock=True)
        if source["spreadsheet_id"] != 0:
            # 迁移窗口前已存在的来源行按其缓存物理位置惰性切换到本地表。
            # 不读取或写入腾讯，只把本地业务表设为唯一来源。
            await cur.execute(
                f"SELECT id FROM `{parser.table_name}` WHERE id=%s LIMIT 1",
                (source["physical_row"],),
            )
            if not await cur.fetchone():
                raise HTTPException(409, "该任务尚未迁移到本地数据源")
            await cur.execute(
                "UPDATE _online_source_rows SET spreadsheet_id=0, sheet_id=%s, "
                "physical_row=%s, source_kind='local_table', source_ref=%s "
                "WHERE id=%s",
                (
                    local_sheet_id(parser_type),
                    source["physical_row"],
                    f"{parser.table_name}:{source['physical_row']}",
                    source_id,
                ),
            )
            source = await _load_source_row(cur, parser_type, source_id, lock=True)
        if source["revision"] != expected_revision:
            raise HTTPException(409, "该任务已被更新，请刷新后重试")
        current_values = {
            column: str(source["values"].get(column, "") or "")
            for column in parser.COLUMNS
        }
        if transaction_prepare is not None:
            prepared_changes = await transaction_prepare(
                cur=cur,
                source=source,
                current_values=current_values,
                changes=dict(normalized_changes),
            )
            if prepared_changes:
                normalized_changes.update({
                    str(column): str(value or "").strip()
                    for column, value in prepared_changes.items()
                })
            unknown = [column for column in normalized_changes if column not in parser.COLUMNS]
            if unknown:
                raise HTTPException(400, f"字段不存在：{'、'.join(unknown)}")
        if current_values_validator is not None:
            current_values_validator(current_values)
        inspector_context = await inspector_option_context(cur, user)
        metadata = await _managed_column_metadata(
            cur,
            parser,
            source.get("cell_meta") or {},
            spreadsheet_id=0,
            sheet_id=local_sheet_id(parser_type),
            inspector_context=inspector_context,
        )
        if source["revision"] != expected_revision:
            submitted_base = {
                str(field): str(value or "").strip()
                for field, value in (base_values or {}).items()
            }
            changed_since_load = [
                field for field in normalized_changes
                if field not in submitted_base
                or not _same_value(
                    current_values.get(field, ""),
                    submitted_base[field],
                    _physical_cell_type(metadata.get(field)),
                )
            ]
            if changed_since_load:
                raise HTTPException(409, {
                    "message": "所编辑字段已被其他平台用户更新，请重新确认",
                    "columns": changed_since_load,
                })
        ordered_columns = [
            column for column in parser.COLUMNS
            if column in normalized_changes
            and not _same_value(
                current_values.get(column, ""),
                normalized_changes[column],
                _physical_cell_type(metadata.get(column)),
            )
        ]
        if not ordered_columns:
            raise HTTPException(400, "提交值与平台当前值相同，无需保存")
        normalized_changes = {column: normalized_changes[column] for column in ordered_columns}
        after = dict(current_values)
        after.update(normalized_changes)
        if not registration_mode:
            from services.task_registration import is_registration_task
            workflow = TASK_WORKFLOWS.get(parser_type)
            if workflow and is_registration_task(parser_type):
                submitted_result = (
                    str(normalized_changes.get(workflow.result_field) or "").strip()
                    if workflow.result_field in normalized_changes else ""
                )
                if submitted_result == "已登记":
                    raise HTTPException(403, "已登记只能由居住证比对闭环或有权复核人员确认")
                if submitted_result == "待登记":
                    raise HTTPException(400, "待登记必须从指令核查页面选择唯一拟登记房屋后统一保存")
        if system_managed_columns is None:
            try:
                await validate_row_changes(cur, user, parser, current_values, after, ordered_columns)
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
        if "核查人" in ordered_columns:
            try:
                validate_inspector_assignment(
                    inspector_context,
                    parser.community_value(after),
                    after.get("核查人"),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if any(column in set(parser.get_business_key()) for column in ordered_columns):
            try:
                parser.validate_existing_row_key(after)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        new_key = parser.make_row_key(after)
        if new_key != source["row_key"]:
            await cur.execute(
                "SELECT id FROM _online_source_rows "
                "WHERE parser_type=%s AND row_key=%s AND id<>%s "
                "AND archived_at IS NULL "
                f"{active_source_sql_filter(parser_type)} LIMIT 1",
                (parser_type, new_key, source_id),
            )
            if await cur.fetchone():
                raise HTTPException(409, "修改后会形成重复业务主键，请先处理重复任务")

        # 同步更新对应的本地业务表和统一来源记录，整个过程在同一事务内完成。
        assignments = ", ".join(f"`{column}`=%s" for column in ordered_columns)
        source_ref = f"{parser.table_name}:{source['physical_row']}"
        await cur.execute(
            "SELECT source_kind,source_ref FROM _online_source_rows WHERE id=%s FOR UPDATE",
            (source_id,),
        )
        previous_source = await cur.fetchone()
        await cur.execute(
            f"UPDATE `{parser.table_name}` SET {assignments}, `_row_key`=%s, _last_updated_at=UTC_TIMESTAMP() "
            "WHERE _row_key=%s",
            [*(after[column] for column in ordered_columns), new_key, source["row_key"]],
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "本地任务已被删除或更新，请刷新后重试")
        await cur.execute(
            "UPDATE _online_source_rows SET row_key=%s,row_hash=%s,values_json=%s,"
            "revision=revision+1,refreshed_at=UTC_TIMESTAMP(),source_kind='local_table',source_ref=%s "
            "WHERE id=%s AND revision=%s",
            (new_key, local_row_hash(after), stable_json(after), source_ref, source_id, expected_revision),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "该任务已被更新，请刷新后重试")
        if previous_source and (
            str(previous_source[0] or "") != "local_table"
            or str(previous_source[1] or "") != source_ref
        ):
            await cur.execute(
                "UPDATE _local_source_records SET status='superseded',updated_at=UTC_TIMESTAMP() "
                "WHERE source_kind=%s AND source_ref=%s",
                (str(previous_source[0] or ""), str(previous_source[1] or "")),
            )
        await cur.execute(
            "UPDATE _local_source_records SET parser_type=%s, local_task_id=%s, "
            "business_key=%s, values_json=%s, content_hash=%s, "
            "revision=revision+1, status='active', archived_at=NULL, "
            "updated_at=UTC_TIMESTAMP() WHERE source_kind='local_table' AND source_ref=%s",
            (
                parser_type, int(source["physical_row"]), new_key,
                stable_json(after), local_row_hash(after), source_ref,
            ),
        )
        if cur.rowcount == 0:
            await cur.execute(
                "INSERT INTO _local_source_records ("
                "parser_type,local_task_id,business_key,source_kind,source_ref,"
                "values_json,content_hash,status,revision) VALUES (%s,%s,%s,'local_table',%s,%s,%s,'active',%s)",
                (
                    parser_type, int(source["physical_row"]), new_key, source_ref,
                    stable_json(after), local_row_hash(after), expected_revision + 1,
                ),
            )
        audit_id = await _insert_writeback_audit(
            cur,
            user=user,
            action=audit_action,
            parser_type=parser_type,
            spreadsheet_id=0,
            sheet_id=local_sheet_id(parser_type),
            physical_row=source["physical_row"],
            column_name="、".join(ordered_columns),
            row_key_before=source["row_key"],
            row_key_after=new_key,
            before_values=None if redact_audit_values else current_values,
            after_values=None if redact_audit_values else after,
            sync_status="local",
        )
        if transaction_callback is not None:
            await transaction_callback(
                cur=cur,
                source=source,
                before=current_values,
                after=after,
                row_key_before=str(source["row_key"]),
                row_key_after=str(new_key),
                revision=expected_revision + 1,
            )
        await migrate_responsibility_row_key(
            cur,
            parser_type,
            str(source["row_key"]),
            str(new_key),
        )
        if (
            "核查人" in ordered_columns
            and not str(current_values.get("核查人") or "").strip()
            and str(after.get("核查人") or "").strip()
        ):
            await capture_first_assignment(
                cur,
                parser_type=parser_type,
                row_key=str(new_key),
                community=parser.community_value(after),
                inspector=str(after.get("核查人") or ""),
                actor_user_id=int(user.get("id")) if user.get("id") else None,
                source="task_assignment",
            )
        if record_unverifiable_save:
            from services.unverifiable_review import record_task_save
            try:
                await record_task_save(
                    cur,
                    parser_type=parser_type,
                    source=source,
                    before=current_values,
                    after=after,
                    changes=normalized_changes,
                    row_key_after=str(new_key),
                    revision=expected_revision + 1,
                    actor_user_id=int(user.get("id")) if user.get("id") else None,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        await rebuild_projection_rows(
            cur,
            parser_type,
            [str(source["row_key"]), str(new_key)],
            reconcile_graph=False,
        )
        await reconcile_online_task_graph(
            cur,
            parser_type=parser_type,
            row_key_before=str(source["row_key"]),
            row_key_after=str(new_key),
            before=current_values,
            after=after,
            actor_user_id=int(user.get("id")) if user.get("id") else None,
            event_type="online_task_save",
        )
        community = parser.community_value(after)
        audiences = ["authenticated"]
        if community:
            audiences.append(f"community:{community}")
        await enqueue_event(
            cur,
            domain="online",
            event_type="online.task.changed",
            aggregate_type="online_task",
            aggregate_id=f"{parser_type}:{new_key}",
            aggregate_revision=expected_revision + 1,
            audiences=audiences,
        )
        activity_credited = await task_update_is_credited_to(
            cur,
            parser_type,
            str(new_key),
            str((user.get("member") or {}).get("name") or ""),
        )
        await conn.commit()
        await record_admin_audit(
            user,
            "online.local_update",
            target_type="local_source_row",
            target_name=f"{parser_type}:{source_id}",
            detail={"source_id": source_id, "columns": ordered_columns},
            **request_audit_fields(request),
        )
        if is_actual_online_work(ordered_columns) and activity_credited:
            await record_work_activity(user, ONLINE_TASK_UPDATE, event_key=f"local:{audit_id}")
        warnings = []
        if "核查人" in parser.COLUMNS and inspector_assignment_mismatch(
            inspector_context,
            parser.community_value(after),
            after.get("核查人"),
        ):
            warnings.append("核查人与当前社区不一致")
        return {
            "message": "已保存到本地业务数据",
            "values": after,
            "row_key": new_key,
            "row_hash": local_row_hash(after),
            "revision": expected_revision + 1,
            "pending_sync": False,
            "warnings": warnings,
            "inspector_mismatch": bool(warnings),
        }


async def update_source_fields(
    *,
    parser_type: str,
    source_id: int,
    changes: dict[str, str],
    expected_revision: int,
    request: Request,
    user: dict,
    conn,
    explicit_text_edit: bool = False,
    allowed_columns: set[str] | None = None,
    current_values_validator=None,
    redact_audit_values: bool = False,
    system_managed_columns: set[str] | None = None,
) -> dict:
    """在一把工作表锁内批量校验、写入并回读同一腾讯来源行。"""
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    if local_data_source_enabled():
        try:
            return await _update_local_source_fields(
                parser_type=parser_type,
                source_id=source_id,
                changes=changes,
                expected_revision=expected_revision,
                request=request,
                user=user,
                conn=conn,
                explicit_text_edit=explicit_text_edit,
                allowed_columns=allowed_columns,
                current_values_validator=current_values_validator,
                redact_audit_values=redact_audit_values,
                system_managed_columns=system_managed_columns,
            )
        except Exception:
            await _connection_transaction_call(conn, "rollback")
            raise
    parser = get_parser(parser_type)
    normalized_changes = {
        str(column): str(value or "").strip()
        for column, value in changes.items()
    }
    if not normalized_changes:
        raise HTTPException(400, "没有需要保存的修改")
    if len(normalized_changes) > 5:
        raise HTTPException(400, "一次最多保存 5 个字段")
    if any(len(value) > 10000 for value in normalized_changes.values()):
        raise HTTPException(400, "单个字段内容不能超过 10000 个字符")
    unknown = [column for column in normalized_changes if column not in parser.COLUMNS]
    if unknown:
        raise HTTPException(400, f"字段不存在：{'、'.join(unknown)}")
    if allowed_columns is not None and any(
        column not in allowed_columns for column in normalized_changes
    ):
        raise HTTPException(400, "提交包含当前入口不允许修改的字段")
    if system_managed_columns is not None and (
        allowed_columns != system_managed_columns
        or set(normalized_changes) != system_managed_columns
    ):
        raise RuntimeError("system-managed edit must use an exact field whitelist")
    ordered_columns = [
        column for column in parser.COLUMNS if column in normalized_changes
    ]

    async with conn.cursor() as cur:
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        source = await _load_source_row(cur, parser_type, source_id)
        if not await acquire_sheet_lock(cur, source["spreadsheet_id"], timeout=2):
            raise HTTPException(409, "该表格正在同步或被他人编辑，请稍后重试")

    client = None
    audit_id = None
    try:
        async with conn.cursor() as cur:
            client = await _oauth_client(cur)
        source_columns = await resolve_source_columns(
            client, source["spreadsheet"], parser
        )
        current_raw = await client.read_source_row(
            source["spreadsheet"]["file_id"],
            source["sheet_id"],
            source["physical_row"],
            source_columns,
        )
        current = {
            **current_raw,
            "values": parser.normalize_source_row(current_raw["values"]),
            "cell_meta": {
                column: (current_raw.get("cell_meta") or {}).get(
                    column, {"type": "text"}
                )
                for column in parser.COLUMNS
            },
        }
        current_values = current["values"]
        if current_values_validator is not None:
            current_values_validator(current_values)
        # Tencent revisions can advance independently of platform edits.  Keep
        # the physical-row identity guard, but do not block on ordinary mirror
        # field changes.
        if parser.make_row_key(current_values) != parser.make_row_key(
            source["values"]
        ):
            await _refresh_spreadsheet(conn, client, source["spreadsheet"])
            raise HTTPException(409, "腾讯表格业务主键已变化，已刷新来源行")

        async with conn.cursor() as cur:
            inspector_context = await inspector_option_context(cur, user)
            current["cell_meta"] = await _managed_column_metadata(
                cur,
                parser,
                current.get("cell_meta") or {},
                spreadsheet_id=source["spreadsheet_id"],
                sheet_id=source["sheet_id"],
                inspector_context=inspector_context,
            )
            ordered_columns = [
                column
                for column in ordered_columns
                if not _same_value(
                    current_values.get(column, ""),
                    normalized_changes[column],
                    _physical_cell_type(current["cell_meta"].get(column)),
                )
            ]
            normalized_changes = {
                column: normalized_changes[column]
                for column in ordered_columns
            }
            if not ordered_columns:
                raise HTTPException(400, "提交值与腾讯当前值相同，无需写回")
            after = dict(current_values)
            after.update(normalized_changes)
            if system_managed_columns is None:
                try:
                    await validate_row_changes(
                        cur, user, parser, current_values, after, ordered_columns
                    )
                except PermissionError as exc:
                    raise HTTPException(403, str(exc)) from exc
            if "核查人" in ordered_columns:
                try:
                    validate_inspector_assignment(
                        inspector_context,
                        parser.community_value(after),
                        after.get("核查人"),
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if any(column in set(parser.get_business_key()) for column in ordered_columns):
                try:
                    parser.validate_existing_row_key(after)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            suspicious_columns = [
                column
                for column in ordered_columns
                if not explicit_text_edit
                and _looks_like_automatic_text_coercion(
                    column,
                    current_values.get(column, ""),
                    normalized_changes[column],
                    _physical_cell_type(current["cell_meta"].get(column)),
                )
            ]
            if suspicious_columns:
                raise HTTPException(
                    400,
                    "检测到身份证、手机号或小数文本疑似被表格自动转换，请重新明确输入完整文本后再保存："
                    + "、".join(suspicious_columns),
                )
            if (
                parser.COMMUNITY_COLUMN in ordered_columns
                and not parser.community_value(after)
            ):
                raise HTTPException(400, "社区不能为空")
            new_key = parser.make_row_key(after)
            if new_key != source["row_key"]:
                await cur.execute(
                    "SELECT id FROM _online_source_rows "
                    "WHERE parser_type=%s AND row_key=%s AND id<>%s LIMIT 1",
                    (parser_type, new_key, source_id),
                )
                if await cur.fetchone():
                    raise HTTPException(409, "修改后会形成重复业务主键，请先处理原始重复行")

            audit_id = await _insert_writeback_audit(
                cur,
                user=user,
                action="update",
                parser_type=parser_type,
                spreadsheet_id=source["spreadsheet_id"],
                sheet_id=source["sheet_id"],
                physical_row=source["physical_row"],
                column_name="、".join(ordered_columns),
                row_key_before=source["row_key"],
                row_key_after=new_key,
                before_values=None if redact_audit_values else current_values,
                after_values=None if redact_audit_values else after,
                sync_status="writing",
            )

        requests = []
        clear_columns: list[int] = []
        try:
            for column in ordered_columns:
                metadata = current["cell_meta"].get(column) or {"type": "text"}
                column_index = source_columns.index(column)
                if not normalized_changes[column]:
                    clear_columns.append(column_index)
                    continue
                requests.append(client.build_update_cell_request(
                    source["sheet_id"],
                    source["physical_row"],
                    column_index,
                    normalized_changes[column],
                    metadata,
                    column,
                ))
        except ValueError as exc:
            if audit_id:
                async with conn.cursor() as cur:
                    await _update_writeback_audit(cur, audit_id, "failed")
            raise HTTPException(400, str(exc)) from exc

        try:
            if requests:
                await client.batch_update(source["spreadsheet"]["file_id"], requests)
            for column_index in clear_columns:
                await client.clear_cell(
                    source["spreadsheet"]["file_id"],
                    source["sheet_id"],
                    source["physical_row"],
                    column_index,
                )
            verified_raw = await client.read_source_row(
                source["spreadsheet"]["file_id"],
                source["sheet_id"],
                source["physical_row"],
                source_columns,
            )
            verified = {
                **verified_raw,
                "values": parser.normalize_source_row(verified_raw["values"]),
                "cell_meta": {
                    column: (verified_raw.get("cell_meta") or {}).get(
                        column, {"type": "text"}
                    )
                    for column in parser.COLUMNS
                },
            }
            mismatched = []
            for column in ordered_columns:
                metadata = current["cell_meta"].get(column) or {"type": "text"}
                if not _same_value(
                    verified["values"].get(column, ""),
                    normalized_changes[column],
                    _physical_cell_type(metadata),
                ):
                    mismatched.append(column)
            if mismatched:
                await _refresh_spreadsheet(conn, client, source["spreadsheet"])
                raise HTTPException(
                    502,
                    f"腾讯表格写入后校验失败：{'、'.join(mismatched)}",
                )
        except TxDocsAPIError as exc:
            if audit_id:
                async with conn.cursor() as cur:
                    await _update_writeback_audit(cur, audit_id, "failed")
            raise HTTPException(502, str(exc)) from exc
        except Exception:
            if audit_id:
                async with conn.cursor() as cur:
                    await _update_writeback_audit(cur, audit_id, "failed")
            raise

        verified_values = verified["values"]
        new_key = parser.make_row_key(verified_values)
        async with conn.cursor() as cur:
            if redact_audit_values:
                await _update_writeback_audit(cur, audit_id, "pending")
            else:
                await _update_writeback_audit(
                    cur,
                    audit_id,
                    "pending",
                    row_key_after=new_key,
                    after_values=verified_values,
                )
        _, revision = await update_cached_source_row(
            conn,
            source_id,
            parser_type,
            verified_values,
            verified["cell_meta"],
        )
    finally:
        try:
            if client:
                await client.close()
        finally:
            async with conn.cursor() as cur:
                await release_sheet_lock(cur, source["spreadsheet_id"])

    await record_admin_audit(
        user,
        "online.writeback.update",
        target_type="online_source_row",
        target_name=f"{parser_type}:{source_id}",
        detail={"source_id": source_id, "columns": ordered_columns},
        **request_audit_fields(request),
    )
    if audit_id and is_actual_online_work(ordered_columns):
        await record_work_activity(
            user,
            ONLINE_TASK_UPDATE,
            event_key=f"writeback:{audit_id}",
        )
    warnings = []
    if "核查人" in parser.COLUMNS and inspector_assignment_mismatch(
        inspector_context,
        parser.community_value(verified_values),
        verified_values.get("核查人"),
    ):
        warnings.append("核查人与当前社区不一致")
    return {
        "message": "已保存，滨湖平台数据已同步更新并写回腾讯表格",
        "values": verified_values,
        "row_key": new_key,
        "revision": revision,
        "pending_sync": True,
        "warnings": warnings,
        "inspector_mismatch": bool(warnings),
    }


async def queue_source_fields(
    *,
    parser_type: str,
    source_id: int,
    changes: dict[str, str],
    base_values: dict[str, str] | None = None,
    expected_revision: int,
    request: Request,
    user: dict,
    conn,
    explicit_text_edit: bool = False,
    allowed_columns: set[str] | None = None,
    current_values_validator=None,
    redact_audit_values: bool = False,
    registration_mode: bool = False,
    audit_action: str = "update",
    transaction_prepare=None,
    transaction_callback=None,
    record_unverifiable_save: bool = True,
) -> dict:
    """先保存平台有效值，再由后台按字段安全写回腾讯。"""
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    if local_data_source_enabled():
        return await _update_local_source_fields(
            parser_type=parser_type,
            source_id=source_id,
            changes=changes,
            expected_revision=expected_revision,
            request=request,
            user=user,
            conn=conn,
            explicit_text_edit=explicit_text_edit,
            allowed_columns=allowed_columns,
            current_values_validator=current_values_validator,
            redact_audit_values=redact_audit_values,
            base_values=base_values,
            registration_mode=registration_mode,
            audit_action=audit_action,
            transaction_prepare=transaction_prepare,
            transaction_callback=transaction_callback,
            record_unverifiable_save=record_unverifiable_save,
        )
    parser = get_parser(parser_type)
    normalized_changes = {
        str(column): str(value or "").strip()
        for column, value in changes.items()
    }
    if not normalized_changes:
        raise HTTPException(400, "没有需要保存的修改")
    if len(normalized_changes) > 5:
        raise HTTPException(400, "一次最多保存 5 个字段")
    if any(len(value) > 10000 for value in normalized_changes.values()):
        raise HTTPException(400, "单个字段内容不能超过 10000 个字符")
    unknown = [column for column in normalized_changes if column not in parser.COLUMNS]
    if unknown:
        raise HTTPException(400, f"字段不存在：{'、'.join(unknown)}")
    if allowed_columns is not None and any(
        column not in allowed_columns for column in normalized_changes
    ):
        raise HTTPException(400, "提交包含当前入口不允许修改的字段")

    await conn.begin()
    try:
        async with conn.cursor() as cur:
            source = await _load_source_row(cur, parser_type, source_id)
            grouped = await load_local_changes(cur, [source_id])
            current_values = overlay_local_values(
                source["values"], grouped.get(source_id, [])
            )
            if current_values_validator is not None:
                current_values_validator(current_values)
            if transaction_prepare is not None:
                prepared_changes = await transaction_prepare(
                    cur=cur,
                    source=source,
                    current_values=current_values,
                    changes=dict(normalized_changes),
                )
                if prepared_changes:
                    normalized_changes.update({
                        str(column): str(value or "").strip()
                        for column, value in prepared_changes.items()
                    })
                if len(normalized_changes) > 5:
                    raise HTTPException(400, "一次最多保存 5 个字段")
                unknown = [
                    column for column in normalized_changes
                    if column not in parser.COLUMNS
                ]
                if unknown:
                    raise HTTPException(400, f"字段不存在：{'、'.join(unknown)}")
            inspector_context = await inspector_option_context(cur, user)
            metadata = await _managed_column_metadata(
                cur,
                parser,
                source.get("cell_meta") or {},
                spreadsheet_id=source["spreadsheet_id"],
                sheet_id=source["sheet_id"],
                inspector_context=inspector_context,
            )
            if source["revision"] != expected_revision:
                submitted_base = {
                    str(field): str(value or "").strip()
                    for field, value in (base_values or {}).items()
                }
                changed_since_load = [
                    field for field in normalized_changes
                    if field not in submitted_base
                    or not _same_value(
                        current_values.get(field, ""),
                        submitted_base[field],
                        _physical_cell_type(metadata.get(field)),
                    )
                ]
                platform_changed_fields = {
                    str(item["field_name"])
                    for item in grouped.get(source_id, [])
                    if str(item.get("status") or "") in {
                        "pending", "processing", "retry", "conflict"
                    }
                }
                changed_by_platform = [
                    field for field in changed_since_load
                    if field in platform_changed_fields
                ]
                if changed_by_platform:
                    raise HTTPException(
                        409,
                        {
                            "message": "所编辑字段已被其他平台用户更新，请重新确认",
                            "columns": changed_by_platform,
                        },
                    )
            ordered_columns = [
                column for column in parser.COLUMNS
                if column in normalized_changes
                and not _same_value(
                    current_values.get(column, ""),
                    normalized_changes[column],
                    _physical_cell_type(metadata.get(column)),
                )
            ]
            if not ordered_columns:
                raise HTTPException(400, "提交值与平台当前值相同，无需保存")
            normalized_changes = {
                column: normalized_changes[column] for column in ordered_columns
            }
            after = dict(current_values)
            after.update(normalized_changes)
            if not registration_mode:
                from services.task_registration import is_registration_task

                workflow = TASK_WORKFLOWS.get(parser_type)
                if workflow and is_registration_task(parser_type):
                    submitted_result = (
                        str(normalized_changes.get(workflow.result_field) or "").strip()
                        if workflow.result_field in normalized_changes else ""
                    )
                    if submitted_result == "已登记":
                        raise HTTPException(
                            403,
                            "已登记只能由居住证比对闭环或有权复核人员确认",
                        )
                    if submitted_result == "待登记":
                        raise HTTPException(
                            400,
                            "待登记必须从指令核查页面选择唯一拟登记房屋后统一保存",
                        )
            try:
                await validate_row_changes(
                    cur, user, parser, current_values, after, ordered_columns
                )
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            if any(column in set(parser.get_business_key()) for column in ordered_columns):
                try:
                    parser.validate_existing_row_key(after)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            suspicious_columns = [
                column
                for column in ordered_columns
                if not explicit_text_edit
                and _looks_like_automatic_text_coercion(
                    column,
                    current_values.get(column, ""),
                    normalized_changes[column],
                    _physical_cell_type(metadata.get(column)),
                )
            ]
            if suspicious_columns:
                raise HTTPException(
                    400,
                    "检测到身份证、手机号或小数文本疑似被表格自动转换，请重新明确输入完整文本后再保存："
                    + "、".join(suspicious_columns),
                )
            if "核查人" in ordered_columns:
                try:
                    validate_inspector_assignment(
                        inspector_context,
                        parser.community_value(after),
                        after.get("核查人"),
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            new_key = parser.make_row_key(after)
            if new_key != source["row_key"]:
                await cur.execute(
                    "SELECT id FROM _online_source_rows "
                    "WHERE parser_type=%s AND row_key=%s AND id<>%s LIMIT 1",
                    (parser_type, new_key, source_id),
                )
                if await cur.fetchone():
                    raise HTTPException(409, "修改后会形成重复业务主键")
            audit_id = await _insert_writeback_audit(
                cur,
                user=user,
                action=audit_action,
                parser_type=parser_type,
                spreadsheet_id=source["spreadsheet_id"],
                sheet_id=source["sheet_id"],
                physical_row=source["physical_row"],
                column_name="、".join(ordered_columns),
                row_key_before=source["row_key"],
                row_key_after=new_key,
                before_values=None if redact_audit_values else current_values,
                after_values=None if redact_audit_values else after,
                sync_status="pending",
            )
            revision = await enqueue_local_changes(
                conn,
                source=source,
                changes=normalized_changes,
                user=user,
                audit_id=audit_id,
            )
            if transaction_callback is not None:
                await transaction_callback(
                    cur=cur,
                    source=source,
                    before=current_values,
                    after=after,
                    row_key_before=str(source["row_key"]),
                    row_key_after=str(new_key),
                    revision=revision,
                )
                # enqueue_local_changes rebuilds once before the registration
                # link is changed.  Rebuild again so task_state and task graph
                # observe the association atomically in this same transaction.
                await rebuild_projection_rows(
                    cur,
                    parser_type,
                    [str(source["row_key"]), str(new_key)],
                    reconcile_graph=False,
                )
            # 结构化“无法核实”流程与所有普通任务保存共用同一事务。
            # 延时期间的二次反馈、正式结果变化和新一轮无法核实都在这里留痕。
            if record_unverifiable_save:
                from services.unverifiable_review import record_task_save
                try:
                    await record_task_save(
                        cur,
                        parser_type=parser_type,
                        source=source,
                        before=current_values,
                        after=after,
                        changes=normalized_changes,
                        row_key_after=str(new_key),
                        revision=revision,
                        actor_user_id=int(user.get("id")) if user.get("id") else None,
                    )
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
            await rebuild_projection_rows(cur, parser_type, [new_key], reconcile_graph=False)
            await enqueue_event(
                cur,
                domain="online",
                event_type="online.task.changed",
                aggregate_type="online_task",
                aggregate_id=f"{parser_type}:{new_key}",
                aggregate_revision=revision,
                audiences=["authenticated"],
            )
            await reconcile_online_task_graph(
                cur,
                parser_type=parser_type,
                row_key_before=str(source["row_key"]),
                row_key_after=str(new_key),
                before=current_values,
                after=after,
                actor_user_id=int(user.get("id")) if user.get("id") else None,
                event_type="online_task_save",
            )
            sync_payload = await source_sync_payload(cur, source_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    await record_admin_audit(
        user,
        "online.writeback.queue",
        target_type="online_source_row",
        target_name=f"{parser_type}:{source_id}",
        detail={"source_id": source_id, "columns": ordered_columns},
        **request_audit_fields(request),
    )
    if is_actual_online_work(ordered_columns):
        await record_work_activity(
            user,
            ONLINE_TASK_UPDATE,
            event_key=f"writeback:{audit_id}",
        )
    launch_local_change_processing(source_id)
    warnings = []
    if "核查人" in parser.COLUMNS and inspector_assignment_mismatch(
        inspector_context,
        parser.community_value(after),
        after.get("核查人"),
    ):
        warnings.append("核查人与当前社区不一致")
    return {
        "message": "已保存到滨湖平台，正在后台同步腾讯表格",
        "values": after,
        "row_key": new_key,
        "revision": revision,
        "pending_sync": bool(sync_payload["state"]),
        "sync_state": sync_payload["state"],
        "warnings": warnings,
        "inspector_mismatch": bool(warnings),
    }


@router.get("/types")
async def get_query_types(
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
):
    del user
    return {"data": QUERY_TYPES}


@router.get("/migration/status")
async def get_local_migration_status(
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """返回本地来源迁移快照状态，不暴露外部凭据或原始数据。"""
    del user
    return await local_source_migration_status(conn)


@router.get("/migration/issues")
async def get_local_migration_issues(
    parser_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """Return the safe migration issue list for the cutover review window."""
    del user
    return await local_source_migration_issues(
        conn,
        parser_type=parser_type,
        page=page,
        page_size=page_size,
    )


@router.get("/writeback/audit")
async def list_writeback_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    parser_type: Optional[str] = Query(None),
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    del user
    where = " WHERE parser_type=%s" if parser_type else ""
    params: list[Any] = [parser_type] if parser_type else []
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_writeback_audit{where}",
            params,
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT id, username, action, parser_type, spreadsheet_id,
                   physical_row, column_name, row_key_before, row_key_after,
                   before_values, after_values, sync_status, synced_at,
                   created_at
            FROM _online_writeback_audit{where}
            ORDER BY id DESC LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = await cur.fetchall()
    return {
        "data": [
            {
                "id": int(row[0]),
                "username": str(row[1]),
                "action": str(row[2]),
                "parser_type": str(row[3]),
                "spreadsheet_id": int(row[4]),
                "physical_row": int(row[5]) if row[5] is not None else None,
                "column_name": str(row[6] or ""),
                "row_key_before": str(row[7] or ""),
                "row_key_after": str(row[8] or ""),
                "before_values": json_value(row[9], {}),
                "after_values": json_value(row[10], {}),
                "sync_status": str(row[11]),
                "synced_at": row[12].isoformat() + "Z" if row[12] else None,
                "created_at": row[13].isoformat() + "Z",
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def _legacy_query(
    *,
    parser_type: str,
    source: str,
    page: int,
    page_size: int,
    keyword: str | None,
    sort_by: str | None,
    sort_order: str,
    filters: str | None,
    grid_filters: str | None,
    user: dict,
    conn,
) -> dict:
    parser = get_parser(parser_type)
    table = parser.table_name
    columns = parser.COLUMNS
    if source == "archive":
        table = f"OnlineDataArchive.{table}_archive"
    column_map = await get_database_column_map(conn, table, parser)

    def database_column(column: str) -> str:
        return quote_identifier(column_map[column])

    where_parts: list[str] = []
    params: list[Any] = []
    scopes = effective_view_communities(user)
    if scopes is not None:
        allowed = await community_names_for_scopes(conn, scopes)
        if not allowed or parser.COMMUNITY_COLUMN not in columns:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(allowed))
            where_parts.append(
                f"{database_column(parser.COMMUNITY_COLUMN)} IN ({placeholders})"
            )
            params.extend(allowed)
    if keyword:
        where_parts.append("(" + " OR ".join(
            f"{database_column(column)} LIKE %s" for column in columns
        ) + ")")
        params.extend([f"%{keyword}%"] * len(columns))
    if filters:
        try:
            parsed_filters = json.loads(filters)
        except (TypeError, json.JSONDecodeError):
            parsed_filters = {}
        for column, values in parsed_filters.items():
            if column not in columns or not isinstance(values, list) or not values:
                continue
            placeholders = ",".join(["%s"] * len(values))
            expression = database_column(column)
            if any(str(value) == "" for value in values):
                expression = f"COALESCE({expression}, '')"
            where_parts.append(f"{expression} IN ({placeholders})")
            params.extend(str(value) for value in values)
    _append_grid_filters(
        where_parts,
        params,
        grid_filters,
        columns,
        database_column,
    )
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
        total = int((await cur.fetchone())[0] or 0)
        order = "ASC" if sort_order == "asc" else "DESC"
        order_clause = (
            f"ORDER BY {database_column(sort_by)} {order}"
            if sort_by in columns else "ORDER BY id DESC"
        )
        col_list = ", ".join(database_column(column) for column in columns)
        await cur.execute(
            f"SELECT {col_list} FROM {table}{where} {order_clause} "
            "LIMIT %s OFFSET %s",
            params + [page_size, (page - 1) * page_size],
        )
        rows = await cur.fetchall()
        # 归档及尚未完成来源定位的旧查询保持只读，不需要额外读取编辑下拉项。
        column_metadata = {column: {"type": "text"} for column in columns}
    result = {
        "data": [
            {
                column: str(row[index]) if row[index] is not None else ""
                for index, column in enumerate(columns)
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "columns": columns,
        "column_meta": [
            {"field": column, **column_metadata[column]} for column in columns
        ],
        "source_ready": False,
        "data_source_mode": "local" if local_data_source_enabled() else "tencent",
        "writeback_enabled": False,
        "can_add": False,
        "required_fields": new_row_required_fields(parser),
        "pending_count": 0,
        "row_manage_message": "",
        "dependent_options": {},
    }
    if scopes == []:
        result["scope_message"] = "当前账号尚未分配社区部门，暂无业务数据"
    elif source == "online":
        result["scope_message"] = "来源定位尚未建立，请等待下一次正常同步后编辑"
    return result


async def _projection_query(
    *,
    parser_type: str,
    page: int,
    page_size: int,
    keyword: str | None,
    sort_by: str | None,
    sort_order: str,
    filters: str | None,
    grid_filters: str | None,
    user: dict,
    conn,
    spreadsheets: list[dict],
) -> dict:
    parser = get_parser(parser_type)
    columns = parser.COLUMNS
    where_parts = ["projection.parser_type=%s"]
    params: list[Any] = [parser_type]
    if local_data_source_enabled():
        # Historical Tencent rows may remain in the projection during the
        # cutover. They are audit-only and must not appear in normal queries.
        where_parts.append(
            "EXISTS (SELECT 1 FROM _online_source_rows AS active_source "
            "WHERE active_source.parser_type=projection.parser_type "
            "AND active_source.row_key=projection.row_key "
            "AND active_source.archived_at IS NULL "
            f"{active_source_sql_filter(parser_type, 'active_source')})"
        )
    scopes = effective_view_communities(user)
    if scopes is not None:
        allowed = await community_names_for_scopes(conn, scopes)
        if not allowed:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(allowed))
            where_parts.append(f"projection.community IN ({placeholders})")
            params.extend(allowed)
    if keyword:
        where_parts.append("projection.search_text LIKE %s")
        params.append(f"%{keyword}%")
    if filters:
        try:
            parsed_filters = json.loads(filters)
        except (TypeError, json.JSONDecodeError):
            parsed_filters = {}
        for column, values in parsed_filters.items():
            if column not in columns or not isinstance(values, list) or not values:
                continue
            expression = (
                "JSON_UNQUOTE(JSON_EXTRACT(projection.values_json, "
                f"'{_json_path(column)}'))"
            )
            placeholders = ",".join(["%s"] * len(values))
            if any(str(value) == "" for value in values):
                expression = f"COALESCE({expression}, '')"
            where_parts.append(f"{expression} IN ({placeholders})")
            params.extend(str(value) for value in values)
    _append_grid_filters(
        where_parts,
        params,
        grid_filters,
        columns,
        lambda column: (
            "JSON_UNQUOTE(JSON_EXTRACT(projection.values_json, "
            f"'{_json_path(column)}'))"
        ),
    )
    where = " WHERE " + " AND ".join(where_parts)
    sort_expression = "projection.updated_at"
    if sort_by in columns:
        sort_expression = (
            "JSON_UNQUOTE(JSON_EXTRACT(projection.values_json, "
            f"'{_json_path(sort_by)}'))"
        )
    order = "ASC" if sort_order == "asc" else "DESC"
    single_source_condition = "projection.source_count=1"
    if local_data_source_enabled():
        single_source_condition = (
            "(SELECT COUNT(*) FROM _online_source_rows AS single_source "
            "WHERE single_source.parser_type=projection.parser_type "
            "AND single_source.row_key=projection.row_key "
            "AND single_source.archived_at IS NULL "
            f"{active_source_sql_filter(parser_type, 'single_source')})=1"
        )

    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection{where}",
            params,
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state,
                   source.id, source.revision, source.row_hash,
                   source.cell_meta_json, source.physical_row
            FROM _online_source_projection AS projection
            LEFT JOIN _online_source_rows AS source
             ON source.parser_type=projection.parser_type
              AND source.row_key=projection.row_key
              AND source.archived_at IS NULL
              {active_source_sql_filter(parser_type, 'source')}
              AND {single_source_condition}
            {where}
            ORDER BY {sort_expression} {order}, projection.row_key
            LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = await cur.fetchall()
        effective_source_counts: dict[str, int] = {}
        if local_data_source_enabled() and rows:
            row_keys = [str(row[0]) for row in rows]
            placeholders = ",".join(["%s"] * len(row_keys))
            await cur.execute(
                "SELECT row_key, COUNT(*) FROM _online_source_rows AS source "
                "WHERE source.parser_type=%s AND source.row_key IN (" + placeholders + ") "
                "AND source.archived_at IS NULL "
                f"{active_source_sql_filter(parser_type, 'source')} "
                "GROUP BY row_key",
                [parser_type, *row_keys],
            )
            effective_source_counts = {
                str(row_key): int(count or 0)
                for row_key, count in await cur.fetchall()
            }
        await cur.execute(
            "SELECT cell_meta_json FROM _online_source_rows "
            "WHERE parser_type=%s AND archived_at IS NULL "
            f"{active_source_sql_filter(parser_type)} ORDER BY id LIMIT 1",
            (parser_type,),
        )
        metadata_row = await cur.fetchone()
        source_metadata = json_value(metadata_row[0], {}) if metadata_row else {}
        inspector_context = await inspector_option_context(cur, user)
        column_metadata = await _managed_column_metadata(
            cur,
            parser,
            source_metadata,
            inspector_context=inspector_context,
        )
        if local_data_source_enabled():
            pending_count = 0
            enabled = True
        else:
            await cur.execute(
                "SELECT COUNT(*) FROM _online_writeback_audit "
                "WHERE parser_type=%s AND sync_status='pending'",
                (parser_type,),
            )
            pending_count = int((await cur.fetchone())[0] or 0)
            enabled = local_data_source_enabled() or await _writeback_enabled(cur)
        data_version = await _source_data_version(cur, parser_type)

        data = []
        for row in rows:
            values = json_value(row[1], {})
            source_count = (
                effective_source_counts.get(str(row[0]), 0)
                if local_data_source_enabled()
                else int(row[2] or 0)
            )
            conflict = (
                source_count > 1
                if local_data_source_enabled()
                else bool(row[3])
            )
            capabilities = await row_edit_capabilities(cur, user, parser, values)
            direct_source = source_count == 1 and row[5] is not None
            editable_fields = (
                capabilities["editable_fields"] if direct_source and not conflict and enabled else []
            )
            record = {
                column: str(values.get(column, "") or "")
                for column in columns
            }
            record.update({
                "__row_key": str(row[0]),
                "__source_count": source_count,
                "__conflict": conflict,
                "__pending": str(row[4] or "") == "pending",
                "__source_id": int(row[5]) if row[5] is not None else None,
                "__revision": int(row[6]) if row[6] is not None else None,
                "__row_hash": str(row[7] or ""),
                "__physical_row": int(row[9]) if row[9] is not None else None,
                "__editable_fields": editable_fields,
                "__can_delete": bool(
                    enabled and direct_source and can_manage_rows(user)
                ),
                "__inspector_mismatch": bool(
                    "核查人" in parser.COLUMNS
                    and inspector_assignment_mismatch(
                        inspector_context,
                        parser.community_value(values),
                        values.get("核查人"),
                    )
                ),
            })
            data.append(record)

    row_manage_allowed = can_manage_rows(user)
    row_manage_message = ""
    if row_manage_allowed and not local_data_source_enabled() and len(spreadsheets) != 1:
        row_manage_message = (
            f"当前业务有 {len(spreadsheets)} 个启用来源表，新增已禁用；"
            "每种业务必须恰好配置一个启用来源表。"
        )

    return {
        "data": data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "columns": columns,
        "column_meta": [
            {"field": column, **column_metadata[column]}
            for column in columns
        ],
        "source_ready": True,
        "data_source_mode": "local" if local_data_source_enabled() else "tencent",
        "writeback_enabled": enabled,
        "can_add": bool(enabled and row_manage_allowed),
        "required_fields": new_row_required_fields(parser),
        "pending_count": pending_count,
        "data_version": data_version,
        "row_manage_message": row_manage_message,
        "dependent_options": inspector_context,
        "scope_message": (
            "当前账号尚未分配社区部门，暂无业务数据" if scopes == [] else ""
        ),
    }


@router.get("/{parser_type}/version")
async def query_data_version(
    parser_type: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    del user
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    async with conn.cursor() as cur:
        return {"data_version": await _source_data_version(cur, parser_type)}


@router.get("/{parser_type}")
async def query_data(
    parser_type: str,
    source: str = Query("online", pattern="^(online|archive)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    keyword: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    filters: Optional[str] = Query(None),
    grid_filters: Optional[str] = Query(None),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, f"不支持的类型: {parser_type}")
    if not isinstance(user, dict):
        user = {"data_scope": "all", "permission_scopes": {}}
    if source == "online":
        if local_data_source_enabled():
            return await _projection_query(
                parser_type=parser_type,
                page=page,
                page_size=page_size,
                keyword=keyword,
                sort_by=sort_by,
                sort_order=sort_order,
                filters=filters,
                grid_filters=grid_filters,
                user=user,
                conn=conn,
                spreadsheets=[],
            )
        async with conn.cursor() as cur:
            spreadsheets = await _enabled_spreadsheets(cur, parser_type)
            ready = await _source_ready(cur, spreadsheets)
        if ready:
            return await _projection_query(
                parser_type=parser_type,
                page=page,
                page_size=page_size,
                keyword=keyword,
                sort_by=sort_by,
                sort_order=sort_order,
                filters=filters,
                grid_filters=grid_filters,
                user=user,
                conn=conn,
                spreadsheets=spreadsheets,
            )
    return await _legacy_query(
        parser_type=parser_type,
        source=source,
        page=page,
        page_size=page_size,
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=filters,
        grid_filters=grid_filters,
        user=user,
        conn=conn,
    )


@router.get("/{parser_type}/rows/{row_key}/sources")
async def list_source_rows(
    parser_type: str,
    row_key: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    parser = get_parser(parser_type)
    scopes = effective_view_communities(user)
    allowed = None
    if scopes is not None:
        allowed = set(await community_names_for_scopes(conn, scopes))
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, physical_row, values_json, cell_meta_json,
                   revision, row_hash
            FROM _online_source_rows AS source
            WHERE source.parser_type=%s AND source.row_key=%s
              AND source.archived_at IS NULL
              {active_source_sql_filter(parser_type)}
            ORDER BY spreadsheet_id, physical_row
            """,
            (parser_type, row_key),
        )
        result = []
        enabled = local_data_source_enabled() or await _writeback_enabled(cur)
        for source_id, physical_row, raw_values, raw_meta, revision, row_hash in await cur.fetchall():
            values = json_value(raw_values, {})
            if allowed is not None and parser.community_value(values) not in allowed:
                continue
            capabilities = await row_edit_capabilities(cur, user, parser, values)
            result.append({
                "id": int(source_id),
                "physical_row": int(physical_row),
                "values": values,
                "cell_meta": json_value(raw_meta, {}),
                "revision": int(revision),
                "row_hash": str(row_hash),
                "editable_fields": capabilities["editable_fields"] if enabled else [],
                "can_delete": bool(enabled and can_manage_rows(user)),
            })
    return {"data": result}


@router.patch("/{parser_type}/source-rows/{source_id}")
async def update_source_cell(
    parser_type: str,
    source_id: int,
    data: CellUpdate,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_RAW_EDIT)),
    conn=Depends(get_db),
):
    # Platform edits are committed locally first; Tencent remains a queued
    # compatibility mirror and must not block ordinary cell editing.
    return await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes={data.column: data.value},
        base_values=None,
        expected_revision=data.expected_revision,
        request=request,
        user=user,
        conn=conn,
        explicit_text_edit=data.explicit_text_edit,
    )


@router.post("/{parser_type}/source-rows")
async def create_source_row(
    parser_type: str,
    data: SourceRowCreate,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_RAW_ROW_MANAGE)),
    conn=Depends(get_db),
):
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    parser = get_parser(parser_type)
    values = {
        column: str(data.values.get(column, "") or "").strip()
        for column in parser.COLUMNS
    }
    try:
        parser.validate_new_row(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if local_data_source_enabled():
        await conn.begin()
    async with conn.cursor() as cur:
        if not local_data_source_enabled() and not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        try:
            formal = await validate_new_row_scope(cur, user, parser, values)
        except (PermissionError, ValueError) as exc:
            status = 403 if isinstance(exc, PermissionError) else 400
            raise HTTPException(status, str(exc)) from exc
        if formal:
            values[parser.COMMUNITY_COLUMN] = formal
        if local_data_source_enabled():
            new_key = parser.make_row_key(values)
            await cur.execute(
                "SELECT id FROM _online_source_rows "
                "WHERE parser_type=%s AND row_key=%s AND archived_at IS NULL "
                f"{active_source_sql_filter(parser_type)} LIMIT 1",
                (parser_type, new_key),
            )
            if await cur.fetchone():
                raise HTTPException(409, "本地任务池中已经存在相同业务主键")
            await cur.execute(
                f"INSERT INTO `{parser.table_name}` (_row_key, "
                + ", ".join(f"`{column}`" for column in parser.COLUMNS)
                + ") VALUES ("
                + ", ".join(["%s"] * (len(parser.COLUMNS) + 1))
                + ")",
                [new_key, *[values[column] for column in parser.COLUMNS]],
            )
            physical_row = int(cur.lastrowid)
            await cur.execute(
                "INSERT INTO _online_source_rows ("
                "spreadsheet_id,parser_type,sheet_id,physical_row,row_key,row_hash,"
                "values_json,cell_meta_json,revision,refreshed_at,source_kind,source_ref"
                ") VALUES (0,%s,%s,%s,%s,%s,%s,%s,1,UTC_TIMESTAMP(),'local_table',%s)",
                (
                    parser_type,
                    local_sheet_id(parser_type),
                    physical_row,
                    new_key,
                    local_row_hash(values),
                    stable_json(values),
                    stable_json({column: {"type": "text"} for column in parser.COLUMNS}),
                    f"{parser.table_name}:{physical_row}",
                ),
            )
            await cur.execute(
                "INSERT INTO _local_source_records ("
                "parser_type,local_task_id,business_key,source_kind,source_ref,"
                "values_json,content_hash,status) VALUES (%s,%s,%s,'local_table',%s,%s,%s,'active')",
                (
                    parser_type, physical_row, new_key,
                    f"{parser.table_name}:{physical_row}",
                    stable_json(values), local_row_hash(values),
                ),
            )
            await rebuild_projection_rows(cur, parser_type, [new_key], reconcile_graph=False)
            await enqueue_event(
                cur,
                domain="online",
                event_type="online.task.created",
                aggregate_type="online_task",
                aggregate_id=f"{parser_type}:{new_key}",
                aggregate_revision=1,
                audiences=["authenticated"],
            )
            # The business-table auto-increment id is only the local physical
            # row used by the compatibility layer.  API callers must receive
            # the stable source-row id used by edit/detail endpoints.
            await cur.execute(
                "SELECT id, revision FROM _online_source_rows "
                "WHERE spreadsheet_id=0 AND parser_type=%s AND row_key=%s "
                "AND archived_at IS NULL LIMIT 1",
                (parser_type, new_key),
            )
            source_row = await cur.fetchone()
            if not source_row:
                raise HTTPException(500, "本地来源记录创建失败")
            await conn.commit()
            await record_admin_audit(
                user,
                "online.local_create",
                target_type="local_source_row",
                target_name=f"{parser_type}:{physical_row}",
                detail={"parser_type": parser_type},
                **request_audit_fields(request),
            )
            return {
                "message": "已创建本地业务数据",
                "source_id": int(source_row[0]),
                "row_key": new_key,
                "revision": int(source_row[1] or 1),
                "values": values,
            }
        spreadsheets = await _enabled_spreadsheets(cur, parser_type)
        if len(spreadsheets) != 1:
            raise HTTPException(409, "该业务没有唯一启用的腾讯来源表，暂时不能新增")
        spreadsheet = spreadsheets[0]
        if not await acquire_sheet_lock(cur, spreadsheet["id"], timeout=2):
            raise HTTPException(409, "该表格正在同步或被他人编辑，请稍后重试")
    client = None
    audit_id = None
    try:
        async with conn.cursor() as cur:
            client = await _oauth_client(cur)
        source_columns = await resolve_source_columns(client, spreadsheet, parser)
        all_rows = await client.read_all_source_rows(
            spreadsheet["file_id"], spreadsheet["data_sheet_id"],
            spreadsheet["header_row"], source_columns,
            include_detected_headers=True,
        )
        source_rows = [row for row in all_rows if not row.get("is_header")]
        new_key = parser.make_row_key(values)
        if any(parser.make_row_key(row["values"]) == new_key for row in source_rows):
            await _refresh_spreadsheet(conn, client, spreadsheet)
            raise HTTPException(409, "腾讯表格中已经存在相同业务主键")
        physical_row = max(
            [spreadsheet["header_row"], *[row["physical_row"] for row in all_rows]]
        ) + 1
        async with conn.cursor() as cur:
            audit_id = await _insert_writeback_audit(
                cur,
                user=user,
                action="create",
                parser_type=parser_type,
                spreadsheet_id=spreadsheet["id"],
                sheet_id=spreadsheet["data_sheet_id"],
                physical_row=physical_row,
                column_name=None,
                row_key_before=None,
                row_key_after=new_key,
                before_values=None,
                after_values=values,
                sync_status="writing",
            )
        try:
            await client.batch_update(
                spreadsheet["file_id"],
                [
                    client.build_update_range_request(
                        spreadsheet["data_sheet_id"],
                        physical_row - 1,
                        0,
                        [parser.source_row_values(values, source_columns)],
                    ),
                ],
            )
            verified = await client.read_source_row(
                spreadsheet["file_id"], spreadsheet["data_sheet_id"],
                physical_row, source_columns,
            )
            verified["values"] = parser.normalize_source_row(verified["values"])
            verified["cell_meta"] = {
                column: (verified.get("cell_meta") or {}).get(
                    column, {"type": "text"}
                )
                for column in parser.COLUMNS
            }
            if not _row_values_match(
                values,
                verified["values"],
                verified["cell_meta"],
                parser.COLUMNS,
            ):
                await _refresh_spreadsheet(conn, client, spreadsheet)
                raise HTTPException(502, "腾讯表格新增后校验失败，请直接检查在线表格")
        except TxDocsAPIError as exc:
            if audit_id:
                async with conn.cursor() as cur:
                    await _update_writeback_audit(cur, audit_id, "failed")
            raise HTTPException(502, str(exc)) from exc
        except Exception:
            if audit_id:
                async with conn.cursor() as cur:
                    await _update_writeback_audit(cur, audit_id, "failed")
            raise
        async with conn.cursor() as cur:
            await _update_writeback_audit(
                cur,
                audit_id,
                "pending",
                row_key_after=new_key,
                after_values=verified["values"],
            )
        await _refresh_spreadsheet(conn, client, spreadsheet)
    finally:
        try:
            if client:
                await client.close()
        finally:
            async with conn.cursor() as cur:
                await release_sheet_lock(cur, spreadsheet["id"])
    await record_admin_audit(
        user,
        "online.writeback.create",
        target_type="online_source_row",
        target_name=parser_type,
        detail={"parser_type": parser_type},
        **request_audit_fields(request),
    )
    return {
        "message": "已新增，滨湖平台数据已同步更新并写回腾讯表格",
        "row_key": new_key,
        "pending_sync": True,
    }


@router.delete("/{parser_type}/source-rows/{source_id}")
async def delete_source_row(
    parser_type: str,
    source_id: int,
    request: Request,
    expected_revision: int = Query(..., gt=0),
    user: dict = Depends(require_permission(ONLINE_RAW_ROW_MANAGE)),
    conn=Depends(get_db),
):
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
    if not can_manage_rows(user):
        raise HTTPException(403, "当前岗位不能新增或删除腾讯原始行")
    parser = get_parser(parser_type)
    if local_data_source_enabled():
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                source = await _load_source_row(cur, parser_type, source_id, lock=True)
                if source["revision"] != expected_revision:
                    raise HTTPException(409, "该任务已被更新，请刷新后重试")
                archive_columns = ["_row_key", *parser.COLUMNS]
                quoted = ", ".join(f"`{column}`" for column in archive_columns)
                await cur.execute(
                    f"INSERT INTO OnlineDataArchive.`{parser.table_name}_archive` ({quoted}) "
                    f"SELECT {quoted} FROM `{parser.table_name}` WHERE id=%s",
                    (source["physical_row"],),
                )
                if cur.rowcount != 1:
                    raise HTTPException(404, "本地任务不存在或已归档")
                await cur.execute(
                    f"DELETE FROM `{parser.table_name}` WHERE id=%s",
                    (source["physical_row"],),
                )
                await cur.execute(
                    "UPDATE _local_source_records SET status='archived', archived_at=UTC_TIMESTAMP(), "
                    "updated_at=UTC_TIMESTAMP() WHERE source_kind=%s AND source_ref=%s",
                    (source["source_kind"], source["source_ref"]),
                )
                await cur.execute("DELETE FROM _online_local_changes WHERE source_id=%s", (source_id,))
                await cur.execute("DELETE FROM _online_source_rows WHERE id=%s", (source_id,))
                await rebuild_projection_rows(cur, parser_type, [str(source["row_key"])])
                await enqueue_event(
                    cur,
                    domain="online",
                    event_type="online.task.deleted",
                    aggregate_type="online_task",
                    aggregate_id=f"{parser_type}:{source['row_key']}",
                    aggregate_revision=source["revision"] + 1,
                    audiences=["authenticated"],
                )
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        await record_admin_audit(
            user,
            "online.local_delete",
            target_type="local_source_row",
            target_name=f"{parser_type}:{source_id}",
            detail={"source_id": source_id},
            **request_audit_fields(request),
        )
        return {"message": "已归档并从本地任务池移除", "pending_sync": False}
    async with conn.cursor() as cur:
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        source = await _load_source_row(cur, parser_type, source_id)
        if source["revision"] != expected_revision:
            raise HTTPException(409, "该行已被更新，请刷新后重试")
        if not await acquire_sheet_lock(cur, source["spreadsheet_id"], timeout=2):
            raise HTTPException(409, "该表格正在同步或被他人编辑，请稍后重试")
    client = None
    audit_id = None
    try:
        async with conn.cursor() as cur:
            client = await _oauth_client(cur)
        source_columns = await resolve_source_columns(
            client, source["spreadsheet"], parser
        )
        current = await client.read_source_row(
            source["spreadsheet"]["file_id"], source["sheet_id"],
            source["physical_row"], source_columns,
        )
        current["values"] = parser.normalize_source_row(current["values"])
        if source_row_hash(current["values"]) != source["row_hash"]:
            await _refresh_spreadsheet(conn, client, source["spreadsheet"])
            raise HTTPException(409, "腾讯表格已被其他人修改，已刷新来源行")
        async with conn.cursor() as cur:
            audit_id = await _insert_writeback_audit(
                cur,
                user=user,
                action="delete",
                parser_type=parser_type,
                spreadsheet_id=source["spreadsheet_id"],
                sheet_id=source["sheet_id"],
                physical_row=source["physical_row"],
                column_name=None,
                row_key_before=source["row_key"],
                row_key_after=None,
                before_values=current["values"],
                after_values=None,
                sync_status="writing",
            )
        try:
            await client.batch_update(
                source["spreadsheet"]["file_id"],
                [client.build_delete_row_request(
                    source["sheet_id"], source["physical_row"]
                )],
            )
            async with conn.cursor() as cur:
                await _update_writeback_audit(cur, audit_id, "pending")
        except TxDocsAPIError as exc:
            async with conn.cursor() as cur:
                await _update_writeback_audit(cur, audit_id, "failed")
            raise HTTPException(502, str(exc)) from exc
        except Exception:
            async with conn.cursor() as cur:
                await _update_writeback_audit(cur, audit_id, "failed")
            raise
        await _refresh_spreadsheet(conn, client, source["spreadsheet"])
    finally:
        try:
            if client:
                await client.close()
        finally:
            async with conn.cursor() as cur:
                await release_sheet_lock(cur, source["spreadsheet_id"])
    await record_admin_audit(
        user,
        "online.writeback.delete",
        target_type="online_source_row",
        target_name=f"{parser_type}:{source_id}",
        detail={"source_id": source_id},
        **request_audit_fields(request),
    )
    return {
        "message": "已删除，滨湖平台数据已同步更新并写回腾讯表格",
        "pending_sync": True,
    }
