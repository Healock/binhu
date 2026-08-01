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


SUMMARY_TYPE = "总汇总表"
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
        JOIN OnlineData._departments AS department
          ON department.id=person.department_id
         AND department.department_type='community'
        JOIN OnlineData._communities AS person_community
          ON person_community.id=department.community_id
        WHERE latest.ledger_rank = 1
          AND latest.included = 1
          AND latest.community <> ''
          AND latest.community <> '社区'
          AND latest.community <> '下发社区'
          {community_clause}
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


async def get_online_overview(
    start_date: str,
    end_date: str,
    parser_type: str,
    community: str | None = None,
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
            parser_types = (
                await _get_summary_types(cur)
                if parser_type == SUMMARY_TYPE
                else [parser_type]
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

            communities = None
            if community is not None:
                if not community:
                    communities = []
                else:
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
                    communities = [
                        str(row[0]).strip()
                        for row in await cur.fetchall()
                    ] or [community]
            tasks = await _load_effective_tasks(
                cur,
                start_date,
                end_date,
                parser_types,
                communities,
            )
            new_keys = await _find_new_activity_keys(cur, tasks, runs)

        total_tasks = len(tasks)
        carryover_tasks = sum(
            1
            for *_, source in tasks
            if str(source) == "carryover"
        )
        new_tasks = sum(
            1
            for parser, row_key, *_ in tasks
            if (str(parser), str(row_key)) in new_keys
        )
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
            "changed_tasks": max(
                total_tasks - carryover_tasks - new_tasks,
                0,
            ),
            "pending_tasks": total_tasks - completed_tasks,
            "completed_tasks": completed_tasks,
            "completion_rate": _ratio(completed_tasks, total_tasks),
        }
    finally:
        pool.release(conn)
