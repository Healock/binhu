"""在线数据查询、腾讯来源行定位与安全回写 API。"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from database import get_db
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
    json_value,
    rebuild_projection,
    release_sheet_lock,
    replace_source_cache,
    resolve_source_columns,
    source_row_hash,
    stable_json,
    update_cached_source_row,
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
from services.task_workflow import TASK_WORKFLOWS
from services.txdocs_client import TxDocsAPIError, TxDocsClient
from services.work_activity import (
    ONLINE_TASK_UPDATE,
    is_actual_online_work,
    record_work_activity,
)


router = APIRouter(
    prefix="/api/query",
    tags=["数据查询"],
    dependencies=[Depends(require_admin_account)],
)
QUERY_TYPES = [item for item in PARSER_REGISTRY if item != "default"]


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
        """
        SELECT COUNT(*), COALESCE(SUM(revision), 0), MAX(refreshed_at)
        FROM _online_source_rows
        WHERE parser_type=%s
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
    return TxDocsClient(str(row[0]), str(row[1]), str(row[2]))


async def _load_source_row(cur, parser_type: str, source_id: int) -> dict:
    await cur.execute(
        """
        SELECT source.id, source.spreadsheet_id, source.sheet_id,
               source.physical_row, source.row_key, source.row_hash,
               source.values_json, source.cell_meta_json, source.revision,
               spreadsheet.name, spreadsheet.file_id,
               spreadsheet.data_sheet_id, spreadsheet.header_row,
               spreadsheet.enabled
        FROM _online_source_rows AS source
        JOIN _config_spreadsheets AS spreadsheet
          ON spreadsheet.id=source.spreadsheet_id
        WHERE source.id=%s AND source.parser_type=%s
        """,
        (source_id, parser_type),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(409, "来源行已经变化，请刷新后重试")
    if not row[13]:
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
        "spreadsheet": {
            "id": int(row[1]),
            "name": str(row[9]),
            "file_id": str(row[10]),
            "data_sheet_id": str(row[11]),
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
) -> dict:
    """在一把工作表锁内批量校验、写入并回读同一腾讯来源行。"""
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
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
    ordered_columns = [
        column for column in parser.COLUMNS if column in normalized_changes
    ]

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
        if source_row_hash(current_values) != source["row_hash"]:
            await _refresh_spreadsheet(conn, client, source["spreadsheet"])
            raise HTTPException(409, "腾讯表格已被其他人修改，已刷新来源行")

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
        try:
            for column in ordered_columns:
                metadata = current["cell_meta"].get(column) or {"type": "text"}
                requests.append(client.build_update_cell_request(
                    source["sheet_id"],
                    source["physical_row"],
                    source_columns.index(column),
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
            await client.batch_update(source["spreadsheet"]["file_id"], requests)
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
) -> dict:
    """先保存平台有效值，再由后台按字段安全写回腾讯。"""
    if parser_type not in QUERY_TYPES:
        raise HTTPException(400, "不支持的业务类型")
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
            if not await _writeback_enabled(cur):
                raise HTTPException(503, "在线回写已由超级管理员暂停")
            source = await _load_source_row(cur, parser_type, source_id)
            grouped = await load_local_changes(cur, [source_id])
            current_values = overlay_local_values(
                source["values"], grouped.get(source_id, [])
            )
            if current_values_validator is not None:
                current_values_validator(current_values)
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
                if changed_since_load:
                    raise HTTPException(
                        409,
                        {
                            "message": "所编辑字段已被其他平台用户更新，请重新确认",
                            "columns": changed_since_load,
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
                sync_status="pending",
            )
            revision = await enqueue_local_changes(
                conn,
                source=source,
                changes=normalized_changes,
                user=user,
                audit_id=audit_id,
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
             AND projection.source_count=1
            {where}
            ORDER BY {sort_expression} {order}, projection.row_key
            LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT cell_meta_json FROM _online_source_rows "
            "WHERE parser_type=%s ORDER BY id LIMIT 1",
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
        await cur.execute(
            "SELECT COUNT(*) FROM _online_writeback_audit "
            "WHERE parser_type=%s AND sync_status='pending'",
            (parser_type,),
        )
        pending_count = int((await cur.fetchone())[0] or 0)
        enabled = await _writeback_enabled(cur)
        data_version = await _source_data_version(cur, parser_type)

        data = []
        for row in rows:
            values = json_value(row[1], {})
            source_count = int(row[2] or 0)
            conflict = bool(row[3])
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
    if row_manage_allowed and len(spreadsheets) != 1:
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
        "writeback_enabled": enabled,
        "can_add": bool(enabled and len(spreadsheets) == 1 and row_manage_allowed),
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
            """
            SELECT id, physical_row, values_json, cell_meta_json,
                   revision, row_hash
            FROM _online_source_rows
            WHERE parser_type=%s AND row_key=%s
            ORDER BY spreadsheet_id, physical_row
            """,
            (parser_type, row_key),
        )
        result = []
        enabled = await _writeback_enabled(cur)
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
    return await update_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes={data.column: data.value},
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
    async with conn.cursor() as cur:
        if not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        try:
            formal = await validate_new_row_scope(cur, user, parser, values)
        except (PermissionError, ValueError) as exc:
            status = 403 if isinstance(exc, PermissionError) else 400
            raise HTTPException(status, str(exc)) from exc
        if formal:
            values[parser.COMMUNITY_COLUMN] = formal
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
