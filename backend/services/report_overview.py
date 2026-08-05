"""在线汇总的数据概览。

概览沿用现有任务流水的区间去重规则，并用相邻快照区分：

- ``carryover``：所选区间开始时已经存在的未完成任务；
- ``new``：该任务在本次首次进入区间时，前一张快照中不存在；
- ``changed``：前一张快照中已经存在，但当天发生了有效业务变化。

本模块只读现有流水和快照，不修改日报或原始数据。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
import re
from typing import Any

from database import db_manager
from services.report_builders import BUILDERS
from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS


SUMMARY_TYPE = "总汇总表"
OVERVIEW_CATEGORIES = {
    "carryover": "结转数据",
    "new": "新下发数据",
    "changed": "已有任务变化",
    "pending": "待完成",
    "completed": "已完成",
}
_SNAPSHOT_TABLE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_snapshot_[A-Za-z0-9]+$"
)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
    )


def _quoted_snapshot(table_name: str) -> str:
    if not _SNAPSHOT_TABLE.fullmatch(table_name):
        raise ValueError(f"非法快照表名：{table_name}")
    return f"`{table_name}`"


def _chunks(values: set[str], size: int = 500):
    materialized = list(values)
    for offset in range(0, len(materialized), size):
        yield materialized[offset : offset + size]


async def _get_summary_types(cur) -> list[str]:
    await cur.execute(
        "SELECT config_value FROM OnlineData._system_config "
        "WHERE config_key='summary_types'"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return list(BUILDERS)
    try:
        configured = json.loads(row[0])
    except Exception:
        return list(BUILDERS)
    return [
        parser_type
        for parser_type in configured
        if parser_type in BUILDERS
    ] or list(BUILDERS)


async def _load_runs(
    cur,
    start_date: str,
    end_date: str,
    parser_types: list[str],
) -> list[tuple[Any, ...]]:
    placeholders = ", ".join(["%s"] * len(parser_types))
    await cur.execute(
        "SELECT report_date, parser_type, snapshot_table, "
        "previous_snapshot_table "
        "FROM _daily_task_ledger_runs "
        f"WHERE report_date BETWEEN %s AND %s "
        f"AND parser_type IN ({placeholders}) "
        "ORDER BY report_date, parser_type",
        (start_date, end_date, *parser_types),
    )
    return list(await cur.fetchall())


async def _load_available_range(
    cur,
    parser_types: list[str],
) -> tuple[Any, Any, int]:
    placeholders = ", ".join(["%s"] * len(parser_types))
    await cur.execute(
        "SELECT MIN(report_date), MAX(report_date), "
        "COUNT(DISTINCT report_date) "
        "FROM _daily_task_ledger_runs "
        f"WHERE parser_type IN ({placeholders})",
        parser_types,
    )
    start_date, end_date, data_days = await cur.fetchone()
    return start_date, end_date, int(data_days or 0)


async def _load_effective_tasks(
    cur,
    start_date: str,
    end_date: str,
    parser_types: list[str],
    communities: list[str] | None = None,
    inspector: str | None = None,
) -> list[tuple[Any, ...]]:
    type_placeholders = ", ".join(["%s"] * len(parser_types))
    community_clause = ""
    community_params: list[str] = []
    if communities is not None:
        if communities:
            community_placeholders = ", ".join(["%s"] * len(communities))
            community_clause = (
                f"AND latest.community IN ({community_placeholders})"
            )
            community_params = communities
        else:
            community_clause = "AND 1=0"
    inspector_clause = ""
    inspector_params: list[str] = []
    if inspector is not None:
        inspector_clause = (
            "AND LOWER(TRIM(latest.inspector))=LOWER(TRIM(%s))"
        )
        inspector_params = [inspector]
    await cur.execute(
        f"""
        WITH latest_ranked AS (
            SELECT
                ledger.*,
                ROW_NUMBER() OVER (
                    PARTITION BY ledger.parser_type, ledger.row_key
                    ORDER BY ledger.report_date DESC, ledger.updated_at DESC
                ) AS ledger_rank
            FROM _daily_task_ledger AS ledger
            WHERE ledger.report_date BETWEEN %s AND %s
              AND ledger.parser_type IN ({type_placeholders})
        ),
        first_ranked AS (
            SELECT
                ledger.parser_type,
                ledger.row_key,
                ledger.report_date,
                ledger.source,
                ROW_NUMBER() OVER (
                    PARTITION BY ledger.parser_type, ledger.row_key
                    ORDER BY ledger.report_date ASC, ledger.updated_at ASC
                ) AS event_rank
            FROM _daily_task_ledger AS ledger
            WHERE ledger.report_date BETWEEN %s AND %s
              AND ledger.parser_type IN ({type_placeholders})
              AND ledger.included = 1
        )
        SELECT
            latest.parser_type,
            latest.row_key,
            latest.task_state,
            first_event.report_date,
            first_event.source
        FROM latest_ranked AS latest
        JOIN first_ranked AS first_event
          ON first_event.parser_type = latest.parser_type
         AND first_event.row_key = latest.row_key
         AND first_event.event_rank = 1
        JOIN OnlineData._grid_members AS person
          ON LOWER(TRIM(person.name)) = LOWER(TRIM(latest.inspector))
        JOIN OnlineData._grid_member_department_links AS person_link
          ON person_link.member_id=person.id
        JOIN OnlineData._departments AS department
          ON department.id=person_link.department_id
         AND department.department_type='community'
        JOIN OnlineData._communities AS person_community
          ON person_community.id=department.community_id
        WHERE latest.ledger_rank = 1
          AND latest.included = 1
          AND latest.community <> ''
          AND latest.community <> '社区'
          AND latest.community <> '下发社区'
          {community_clause}
          {inspector_clause}
          AND person.position IN ('组长', '组员')
        """,
        (
            start_date,
            end_date,
            *parser_types,
            start_date,
            end_date,
            *parser_types,
            *community_params,
            *inspector_params,
        ),
    )
    return list(await cur.fetchall())


async def _find_new_activity_keys(
    cur,
    tasks: list[tuple[Any, ...]],
    runs: list[tuple[Any, ...]],
) -> set[tuple[str, str]]:
    run_map = {
        (str(report_date), str(parser_type)): (
            str(snapshot_table),
            str(previous_snapshot) if previous_snapshot else None,
        )
        for (
            report_date,
            parser_type,
            snapshot_table,
            previous_snapshot,
        ) in runs
    }
    activity_by_run: dict[tuple[str, str], set[str]] = defaultdict(set)
    for parser_type, row_key, _, first_date, source in tasks:
        if str(source) == "activity":
            activity_by_run[
                (str(first_date), str(parser_type))
            ].add(str(row_key))

    new_keys: set[tuple[str, str]] = set()
    for run_key, row_keys in activity_by_run.items():
        snapshot_pair = run_map.get(run_key)
        if not snapshot_pair:
            continue
        snapshot_table, previous_snapshot = snapshot_pair
        if previous_snapshot is None:
            new_keys.update(
                (run_key[1], row_key)
                for row_key in row_keys
            )
            continue

        today = _quoted_snapshot(snapshot_table)
        previous = _quoted_snapshot(previous_snapshot)
        for row_key_chunk in _chunks(row_keys):
            placeholders = ", ".join(["%s"] * len(row_key_chunk))
            await cur.execute(
                f"""
                SELECT today._row_key
                FROM {today} AS today
                LEFT JOIN {previous} AS previous
                  ON previous._row_key = today._row_key
                WHERE previous._row_key IS NULL
                  AND today._row_key IN ({placeholders})
                """,
                row_key_chunk,
            )
            new_keys.update(
                (run_key[1], str(row[0]))
                for row in await cur.fetchall()
            )
    return new_keys


def _task_change_category(
    task: tuple[Any, ...],
    new_keys: set[tuple[str, str]],
) -> str:
    parser_type, row_key, _, _, source = task
    if str(source) == "carryover":
        return "carryover"
    if (str(parser_type), str(row_key)) in new_keys:
        return "new"
    return "changed"


def _filter_tasks_by_category(
    tasks: list[tuple[Any, ...]],
    new_keys: set[tuple[str, str]],
    category: str,
) -> list[tuple[Any, ...]]:
    if category not in OVERVIEW_CATEGORIES:
        raise ValueError("不支持的数据概览分类")
    if category == "completed":
        return [task for task in tasks if str(task[2]) == "completed"]
    if category == "pending":
        return [task for task in tasks if str(task[2]) != "completed"]
    return [
        task
        for task in tasks
        if _task_change_category(task, new_keys) == category
    ]


async def _resolve_parser_types(
    cur,
    parser_type: str,
    parser_types_override: list[str] | None,
) -> list[str]:
    parser_types = (
        list(dict.fromkeys(parser_types_override))
        if parser_types_override
        else (
            await _get_summary_types(cur)
            if parser_type == SUMMARY_TYPE
            else [parser_type]
        )
    )
    if any(item not in BUILDERS for item in parser_types):
        raise ValueError("包含未实现日报的业务类型")
    return parser_types


async def _resolve_communities(
    cur,
    community: str | list[str] | None,
) -> list[str] | None:
    if isinstance(community, list):
        return list(dict.fromkeys(
            str(value).strip() for value in community
            if str(value).strip()
        ))
    if community is None:
        return None
    if not community:
        return []
    await cur.execute(
        """
        SELECT c.name
        FROM OnlineData._communities AS c
        WHERE c.name=%s
        UNION
        SELECT a.alias
        FROM OnlineData._community_aliases AS a
        JOIN OnlineData._communities AS c
          ON c.id=a.community_id
        WHERE c.name=%s
        """,
        (community, community),
    )
    return [
        str(row[0]).strip()
        for row in await cur.fetchall()
    ] or [community]


async def _load_latest_task_metadata(
    cur,
    start_date: str,
    end_date: str,
    tasks: list[tuple[Any, ...]],
) -> dict[tuple[str, str], dict[str, str]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    keys_by_type: dict[str, set[str]] = defaultdict(set)
    for parser_type, row_key, *_ in tasks:
        keys_by_type[str(parser_type)].add(str(row_key))
    for parser_type, row_keys in keys_by_type.items():
        for row_key_chunk in _chunks(row_keys):
            placeholders = ", ".join(["%s"] * len(row_key_chunk))
            await cur.execute(
                f"""
                WITH ranked AS (
                    SELECT ledger.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY ledger.parser_type, ledger.row_key
                               ORDER BY ledger.report_date DESC,
                                        ledger.updated_at DESC
                           ) AS ledger_rank
                    FROM _daily_task_ledger AS ledger
                    WHERE ledger.report_date BETWEEN %s AND %s
                      AND ledger.parser_type=%s
                      AND ledger.row_key IN ({placeholders})
                )
                SELECT row_key, report_date, community, inspector, task_state
                FROM ranked
                WHERE ledger_rank=1 AND included=1
                """,
                (start_date, end_date, parser_type, *row_key_chunk),
            )
            rows = await cur.fetchall()
            for row_key, report_date, community, inspector, state in rows:
                result[(parser_type, str(row_key))] = {
                    "report_date": str(report_date),
                    "community": str(community or ""),
                    "inspector": str(inspector or ""),
                    "state": str(state or ""),
                }
    return result


def _snapshot_values(
    parser,
    column_names: list[str],
    row: tuple[Any, ...],
) -> dict[str, str]:
    raw = dict(zip(column_names, row))
    values: dict[str, str] = {}
    for column in parser.COLUMNS:
        candidates = (column, *parser.DATABASE_COLUMN_ALIASES.get(column, ()))
        value = next(
            (
                raw.get(candidate)
                for candidate in candidates
                if candidate in raw
            ),
            "",
        )
        values[column] = str(value or "")
    return values


async def _load_snapshot_values(
    cur,
    tasks: list[tuple[Any, ...]],
    metadata: dict[tuple[str, str], dict[str, str]],
    runs: list[tuple[Any, ...]],
) -> dict[tuple[str, str], dict[str, str]]:
    run_tables = {
        (str(report_date), str(parser_type)): str(snapshot_table)
        for report_date, parser_type, snapshot_table, _ in runs
    }
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for parser_type, row_key, *_ in tasks:
        key = (str(parser_type), str(row_key))
        task_meta = metadata.get(key)
        if not task_meta:
            continue
        table = run_tables.get((task_meta["report_date"], key[0]))
        if table:
            grouped[(key[0], table)].add(key[1])

    result: dict[tuple[str, str], dict[str, str]] = {}
    for (parser_type, table_name), row_keys in grouped.items():
        table = _quoted_snapshot(table_name)
        parser = get_parser(parser_type)
        for row_key_chunk in _chunks(row_keys):
            placeholders = ", ".join(["%s"] * len(row_key_chunk))
            await cur.execute(
                f"SELECT * FROM {table} WHERE _row_key IN ({placeholders})",
                row_key_chunk,
            )
            column_names = [str(item[0]) for item in (cur.description or [])]
            for row in await cur.fetchall():
                raw = dict(zip(column_names, row))
                row_key = str(raw.get("_row_key") or "")
                if row_key:
                    result[(parser_type, row_key)] = _snapshot_values(
                        parser, column_names, row
                    )
    return result


def _category_reason(category: str, state: str) -> str:
    if category == "carryover":
        return "进入所选区间时已经存在且尚未完成"
    if category == "new":
        return "首次进入所选区间，前一张快照中不存在"
    if category == "changed":
        return "已有任务在所选区间内发生有效变化"
    if category == "completed":
        return "区间最终状态为已完成"
    if state == "checked":
        return "已经核查，但仍需补充最终结果"
    return "区间最终状态尚未完成"


def _first_dispatch_date(
    values: dict[str, str],
    first_activity_date: str,
) -> str:
    """返回业务表记录的首次下发日期，旧表无该字段时兼容回退。"""
    for field in ("下发日期", "下发时间"):
        value = str(values.get(field) or "").strip()
        if value:
            return value
    return str(first_activity_date)


async def get_online_overview_details(
    start_date: str,
    end_date: str,
    parser_type: str,
    category: str,
    *,
    page: int = 1,
    page_size: int = 20,
    community: str | list[str] | None = None,
    inspector: str | None = None,
    parser_types_override: list[str] | None = None,
) -> dict[str, Any]:
    """按概览完全相同的口径返回去重后的任务明细。"""
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    if parser_type != SUMMARY_TYPE and parser_type not in BUILDERS:
        raise ValueError(f"未实现的类型：{parser_type}")
    if category not in OVERVIEW_CATEGORIES:
        raise ValueError("不支持的数据概览分类")
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("分页参数不正确")

    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            parser_types = await _resolve_parser_types(
                cur, parser_type, parser_types_override
            )
            runs = await _load_runs(cur, start_date, end_date, parser_types)
            if not runs:
                return {
                    "category": category,
                    "category_label": OVERVIEW_CATEGORIES[category],
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                    "data": [],
                }
            communities = await _resolve_communities(cur, community)
            tasks = await _load_effective_tasks(
                cur,
                start_date,
                end_date,
                parser_types,
                communities,
                inspector,
            )
            new_keys = await _find_new_activity_keys(cur, tasks, runs)
            filtered = _filter_tasks_by_category(tasks, new_keys, category)
            filtered.sort(
                key=lambda task: (str(task[3]), str(task[0]), str(task[1])),
                reverse=True,
            )
            total = len(filtered)
            offset = (page - 1) * page_size
            selected = filtered[offset : offset + page_size]
            metadata = await _load_latest_task_metadata(
                cur, start_date, end_date, selected
            )
            snapshots = await _load_snapshot_values(
                cur, selected, metadata, runs
            )

        data = []
        for task in selected:
            parser_name, row_key, task_state, first_date, _ = task
            key = (str(parser_name), str(row_key))
            task_meta = metadata.get(key, {})
            values = snapshots.get(key, {})
            workflow = TASK_WORKFLOWS[str(parser_name)]
            summary = workflow.summary(values)
            state = str(task_meta.get("state") or task_state or "")
            data.append({
                "parser_type": str(parser_name),
                "row_key": str(row_key),
                "community": str(task_meta.get("community") or ""),
                "inspector": str(task_meta.get("inspector") or ""),
                "state": state,
                "first_activity_date": str(first_date),
                "first_dispatch_date": _first_dispatch_date(
                    values, str(first_date)
                ),
                "last_activity_date": str(task_meta.get("report_date") or first_date),
                "reason": _category_reason(category, state),
                "summary": summary,
                "values": values,
            })
        return {
            "category": category,
            "category_label": OVERVIEW_CATEGORIES[category],
            "total": total,
            "page": page,
            "page_size": page_size,
            "data": data,
        }
    finally:
        pool.release(conn)


async def get_online_overview(
    start_date: str,
    end_date: str,
    parser_type: str,
    community: str | list[str] | None = None,
    inspector: str | None = None,
    parser_types_override: list[str] | None = None,
) -> dict[str, Any]:
    """返回跟随业务类型和日期区间变化的在线数据概览。"""
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    if parser_type != SUMMARY_TYPE and parser_type not in BUILDERS:
        raise ValueError(f"未实现的类型：{parser_type}")

    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            parser_types = await _resolve_parser_types(
                cur, parser_type, parser_types_override
            )
            available_start, available_end, available_days = (
                await _load_available_range(cur, parser_types)
            )
            runs = await _load_runs(
                cur,
                start_date,
                end_date,
                parser_types,
            )
            if not runs:
                return {
                    "exists": False,
                    "parser_type": parser_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "available_start_date": (
                        str(available_start)
                        if available_start
                        else None
                    ),
                    "available_end_date": (
                        str(available_end)
                        if available_end
                        else None
                    ),
                    "available_data_days": available_days,
                    "selected_data_days": 0,
                    "total_tasks": 0,
                    "carryover_tasks": 0,
                    "new_tasks": 0,
                    "changed_tasks": 0,
                    "pending_tasks": 0,
                    "completed_tasks": 0,
                    "completion_rate": 0.0,
                }

            communities = await _resolve_communities(cur, community)
            tasks = await _load_effective_tasks(
                cur,
                start_date,
                end_date,
                parser_types,
                communities,
                inspector,
            )
            new_keys = await _find_new_activity_keys(cur, tasks, runs)

        total_tasks = len(tasks)
        carryover_tasks = len(_filter_tasks_by_category(tasks, new_keys, "carryover"))
        new_tasks = len(_filter_tasks_by_category(tasks, new_keys, "new"))
        changed_tasks = len(_filter_tasks_by_category(tasks, new_keys, "changed"))
        completed_tasks = sum(
            1
            for _, _, state, _, _ in tasks
            if str(state) == "completed"
        )
        return {
            "exists": True,
            "parser_type": parser_type,
            "start_date": start_date,
            "end_date": end_date,
            "available_start_date": (
                str(available_start) if available_start else None
            ),
            "available_end_date": (
                str(available_end) if available_end else None
            ),
            "available_data_days": available_days,
            "selected_data_days": len({
                str(run[0])
                for run in runs
            }),
            "total_tasks": total_tasks,
            "carryover_tasks": carryover_tasks,
            "new_tasks": new_tasks,
            "changed_tasks": changed_tasks,
            "pending_tasks": total_tasks - completed_tasks,
            "completed_tasks": completed_tasks,
            "completion_rate": _ratio(completed_tasks, total_tasks),
        }
    finally:
        pool.release(conn)
