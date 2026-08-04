"""在线数据区间汇总。

区间统计只读取 ``_daily_task_ledger``：
- 区间开始时尚未完成的遗留任务会由当天 carryover 流水带入；
- 同一业务、同一 ``_row_key`` 在区间内只保留最后一条流水；
- 最后一条是未完成移除记录时排除，已完成后再移除则保留完成记录。
"""

import json

from database import db_manager
from services.report_builders import BUILDERS
from services.report_members import (
    aggregate_community_rows,
    calculate_ratio,
    canonical_community,
    canonicalize_inspector_rows,
    complete_inspector_rows,
    get_active_members,
    get_community_alias_lookup,
    merge_inspector_rows,
)
from services.report_attendance import (
    load_community_person_days as _load_community_person_days,
)
from services.report_workload import load_effective_workload_by_community


INSPECTOR_COLUMNS = [
    "社区",
    "姓名",
    "数据总数",
    "未核查",
    "已核查",
    "已完成",
    "核查完成率",
    "无法见底数",
    "核查见底率",
]

COMMUNITY_COLUMNS = [
    "社区",
    "数据总数",
    "未核查",
    "已核查",
    "已完成",
    "核查完成率",
    "无法见底数",
    "核查见底率",
]


async def _get_summary_types(cur) -> list[str]:
    """从系统配置读取总汇总表使用的分表类型。"""
    await cur.execute(
        "SELECT config_value FROM OnlineData._system_config "
        "WHERE config_key = 'summary_types'"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return list(BUILDERS.keys())
    try:
        types = json.loads(row[0])
        return [
            parser_type
            for parser_type in types
            if parser_type in BUILDERS
        ] or list(BUILDERS.keys())
    except Exception:
        return list(BUILDERS.keys())


async def _find_snapshot_dates(
    cur,
    start_date: str,
    end_date: str,
    parser_type: str,
) -> list[str]:
    await cur.execute(
        "SELECT DISTINCT report_date FROM _daily_report_meta "
        "WHERE report_date BETWEEN %s AND %s AND parser_type = %s "
        "ORDER BY report_date",
        (start_date, end_date, f"{parser_type}_snapshot"),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def _find_ledger_dates(
    cur,
    start_date: str,
    end_date: str,
    parser_type: str,
) -> list[str]:
    await cur.execute(
        "SELECT report_date FROM _daily_task_ledger_runs "
        "WHERE report_date BETWEEN %s AND %s AND parser_type = %s "
        "ORDER BY report_date",
        (start_date, end_date, parser_type),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def _validate_ledger_coverage(
    cur,
    start_date: str,
    end_date: str,
    parser_type: str,
) -> tuple[list[str], list[str]]:
    """返回快照日期和缺少流水的日期。"""
    snapshot_dates = await _find_snapshot_dates(
        cur,
        start_date,
        end_date,
        parser_type,
    )
    if not snapshot_dates:
        return [], []
    ledger_dates = set(
        await _find_ledger_dates(
            cur,
            start_date,
            end_date,
            parser_type,
        )
    )
    missing = [
        report_date
        for report_date in snapshot_dates
        if report_date not in ledger_dates
    ]
    return snapshot_dates, missing


async def _aggregate_range_ledger(
    cur,
    start_date: str,
    end_date: str,
    parser_type: str,
) -> list[tuple]:
    """按任务最后状态去重后，聚合为现有人员日报列。"""
    await cur.execute(
        """
        SELECT
            latest.community,
            latest.inspector,
            COUNT(*) AS total_count,
            SUM(latest.task_state = 'unchecked') AS unchecked_count,
            SUM(latest.task_state = 'checked') AS checked_count,
            SUM(latest.task_state = 'completed') AS completed_count,
            CASE WHEN COUNT(*) > 0
                 THEN ROUND(
                    SUM(latest.task_state = 'completed') / COUNT(*),
                    2
                 )
                 ELSE 0 END AS completion_rate,
            SUM(latest.unable_to_verify) AS unable_count,
            CASE WHEN SUM(latest.task_state = 'completed')
                           + SUM(latest.unable_to_verify) > 0
                 THEN ROUND(
                    SUM(latest.task_state = 'completed')
                    / (SUM(latest.task_state = 'completed')
                       + SUM(latest.unable_to_verify)),
                    2
                 )
                 ELSE 0 END AS reached_bottom_rate
        FROM (
            SELECT ranked.*
            FROM (
                SELECT
                    ledger.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY ledger.parser_type, ledger.row_key
                        ORDER BY ledger.report_date DESC, ledger.updated_at DESC
                    ) AS ledger_rank
                FROM _daily_task_ledger ledger
                WHERE ledger.report_date BETWEEN %s AND %s
                  AND ledger.parser_type = %s
            ) ranked
            WHERE ranked.ledger_rank = 1
              AND ranked.included = 1
        ) latest
        WHERE latest.inspector <> ''
          AND latest.inspector <> '核查人'
          AND latest.community <> ''
          AND latest.community <> '社区'
          AND latest.community <> '下发社区'
        GROUP BY latest.community, latest.inspector
        ORDER BY latest.community, latest.inspector
        """,
        (start_date, end_date, parser_type),
    )
    return list(await cur.fetchall())


async def get_report_range(
    start_date: str,
    end_date: str,
    parser_type: str,
) -> dict:
    """查询某个业务的区间汇总。"""
    if parser_type not in BUILDERS:
        return {
            "exists": False,
            "message": f"未实现的类型：{parser_type}",
        }

    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            snapshot_dates, missing_dates = await _validate_ledger_coverage(
                cur,
                start_date,
                end_date,
                parser_type,
            )
            if not snapshot_dates:
                return {
                    "exists": False,
                    "message": (
                        f"{start_date} 至 {end_date} 没有同步快照，"
                        "暂无统计数据"
                    ),
                }
            if missing_dates:
                return {
                    "exists": False,
                    "message": (
                        "所选日期的任务流水尚未生成，请先完成历史回算："
                        + "、".join(missing_dates)
                    ),
                }

            inspector_rows = await _aggregate_range_ledger(
                cur,
                start_date,
                end_date,
                parser_type,
            )
            alias_lookup = await get_community_alias_lookup(cur)
            inspector_rows = canonicalize_inspector_rows(
                inspector_rows,
                alias_lookup,
            )
            inspector_rows = await complete_inspector_rows(
                cur,
                inspector_rows,
                end_date,
            )
            inspector_rows = canonicalize_inspector_rows(
                inspector_rows,
                alias_lookup,
            )
            community_rows = aggregate_community_rows(inspector_rows)

        return {
            "exists": True,
            "range": {
                "start": start_date,
                "end": end_date,
                "days": len(snapshot_dates),
            },
            "inspector": {
                "columns": INSPECTOR_COLUMNS,
                "data": [
                    dict(zip(INSPECTOR_COLUMNS, row))
                    for row in inspector_rows
                ],
            },
            "community": {
                "columns": COMMUNITY_COLUMNS,
                "data": [
                    dict(zip(COMMUNITY_COLUMNS, row))
                    for row in community_rows
                ],
            },
        }
    finally:
        pool.release(conn)


async def get_summary_range(start_date: str, end_date: str) -> dict:
    """查询区间总汇总，并按姓名合并各业务人员数据。"""
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            summary_types = await _get_summary_types(cur)
            alias_lookup = await get_community_alias_lookup(cur)
            all_inspector_rows = []
            covered_dates: set[str] = set()

            for parser_type in summary_types:
                snapshot_dates, missing_dates = (
                    await _validate_ledger_coverage(
                        cur,
                        start_date,
                        end_date,
                        parser_type,
                    )
                )
                if not snapshot_dates:
                    continue
                if missing_dates:
                    return {
                        "exists": False,
                        "message": (
                            f"「{parser_type}」的任务流水尚未生成："
                            + "、".join(missing_dates)
                        ),
                    }

                covered_dates.update(snapshot_dates)
                rows = await _aggregate_range_ledger(
                    cur,
                    start_date,
                    end_date,
                    parser_type,
                )
                rows = canonicalize_inspector_rows(
                    rows,
                    alias_lookup,
                )
                rows = await complete_inspector_rows(
                    cur,
                    rows,
                    end_date,
                )
                rows = canonicalize_inspector_rows(
                    rows,
                    alias_lookup,
                )
                all_inspector_rows.extend(rows)

            if not covered_dates:
                return {
                    "exists": False,
                    "message": (
                        f"{start_date} 至 {end_date} 没有同步快照，"
                        "暂无总汇总数据"
                    ),
                }

            active_members = [
                (
                    canonical_community(community, alias_lookup),
                    name,
                )
                for community, name in await get_active_members(
                    cur,
                    end_date,
                )
            ]
            inspector_rows = merge_inspector_rows(
                all_inspector_rows,
                active_members,
            )
            merged_rows = aggregate_community_rows(inspector_rows)

            member_counts: dict[str, int] = {}
            for community, _ in active_members:
                member_counts[community] = (
                    member_counts.get(community, 0) + 1
                )

            community_person_days, attendance = (
                await _load_community_person_days(
                    covered_dates,
                    alias_lookup,
                )
            )
            effective_workload = await load_effective_workload_by_community(
                cur,
                start_date,
                end_date,
                summary_types,
            )

            community_rows = []
            for row in merged_rows:
                community = str(row[0])
                member_count = member_counts.get(community, 0)
                person_days = community_person_days.get(community, 0)
                community_rows.append(
                    (
                        *row,
                        member_count,
                        person_days if attendance["complete"] else None,
                        (
                            calculate_ratio(
                                effective_workload.get(community, 0),
                                person_days,
                            )
                            if attendance["complete"]
                            else None
                        ),
                    )
                )

        community_columns = [
            *COMMUNITY_COLUMNS,
            "网格员人数",
            "在岗人日",
            "每日人均核查数",
        ]
        inspector_table = {
            "columns": INSPECTOR_COLUMNS,
            "data": [
                dict(zip(INSPECTOR_COLUMNS, row))
                for row in inspector_rows
            ],
        }
        community_table = {
            "columns": community_columns,
            "data": [
                dict(zip(community_columns, row))
                for row in community_rows
            ],
        }
        return {
            "exists": True,
            "range": {
                "start": start_date,
                "end": end_date,
                "days": len(covered_dates),
            },
            "attendance": attendance,
            **community_table,
            "inspector": inspector_table,
            "community": community_table,
        }
    finally:
        pool.release(conn)
