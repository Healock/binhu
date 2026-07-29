"""在线日报的跨日任务流水与聚合。"""

import re
from datetime import date
from typing import Any

from services.business_time import (
    get_business_date,
    get_business_date_range_utc_bounds,
)
from services.report_members import (
    insert_zero_member_rows,
    rebuild_community_report_table,
)


_SNAPSHOT_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}_snapshot_[A-Za-z0-9]+$"
)
_SOURCE_TABLE = re.compile(r"^t_[a-z0-9_]+$")


def _snapshot_identifier(table_name: str) -> str:
    if not _SNAPSHOT_NAME.fullmatch(table_name):
        raise ValueError(f"非法快照表名: {table_name}")
    return f"`{table_name}`"


def _source_identifier(table_name: str) -> str:
    if not _SOURCE_TABLE.fullmatch(table_name):
        raise ValueError(f"非法业务表名: {table_name}")
    return f"`{table_name}`"


async def _find_previous_snapshot(
    cur,
    report_date: str,
    parser_type: str,
) -> str | None:
    await cur.execute(
        "SELECT table_name FROM _daily_report_meta "
        "WHERE report_date < %s AND parser_type = %s "
        "ORDER BY report_date DESC LIMIT 1",
        (report_date, f"{parser_type}_snapshot"),
    )
    row = await cur.fetchone()
    return str(row[0]) if row else None


def _date_activity_sql(alias: str) -> str:
    return (
        f"(({alias}._first_seen_at >= %s AND {alias}._first_seen_at < %s) "
        f"OR ({alias}._last_updated_at >= %s "
        f"AND {alias}._last_updated_at < %s))"
    )


async def refresh_daily_ledger(
    cur,
    builder: Any,
    report_date: str,
    *,
    generation_method: str = "sync",
    reset: bool | None = None,
) -> dict:
    """根据当天和前一张可用快照刷新逐任务流水。"""
    date.fromisoformat(report_date)
    snapshot_name = f"{report_date}_snapshot_{builder.table_suffix}"
    today = _snapshot_identifier(snapshot_name)
    await cur.execute(
        "SELECT generated_at FROM _daily_report_meta "
        "WHERE table_name=%s AND report_date=%s",
        (snapshot_name, report_date),
    )
    snapshot_meta = await cur.fetchone()
    if not snapshot_meta or snapshot_meta[0] is None:
        raise RuntimeError(
            f"{snapshot_name} 缺少可用的快照生成时间"
        )
    snapshot_generated_at = snapshot_meta[0]
    previous_name = await _find_previous_snapshot(
        cur,
        report_date,
        builder.parser_type,
    )
    previous = (
        _snapshot_identifier(previous_name)
        if previous_name
        else None
    )
    source_table = _source_identifier(builder.source_table)
    archive_table = _source_identifier(f"{builder.source_table}_archive")

    current_business_day = await get_business_date(cur)
    is_current_day = report_date == current_business_day.isoformat()
    if reset is None:
        reset = not is_current_day
    if reset:
        await cur.execute(
            "DELETE FROM _daily_task_ledger "
            "WHERE report_date=%s AND parser_type=%s",
            (report_date, builder.parser_type),
        )

    parameters: list[Any] = []
    if is_current_day:
        online_sql = (
            "EXISTS (SELECT 1 FROM OnlineData."
            f"{source_table} live WHERE live._row_key=t._row_key)"
        )
    else:
        _, utc_end = await get_business_date_range_utc_bounds(
            cur,
            report_date,
            report_date,
        )
        online_sql = (
            "NOT EXISTS (SELECT 1 FROM OnlineDataArchive."
            f"{archive_table} archived "
            "WHERE archived._row_key=t._row_key "
            "AND archived._archived_at >= %s "
            "AND archived._archived_at < %s)"
        )
        parameters.extend([snapshot_generated_at, utc_end])

    state_sql = builder.ledger_state_sql("t")
    unable_sql = builder.ledger_unable_sql("t")
    reached_bottom_sql = builder.ledger_reached_bottom_sql("t")
    community = builder.community_column
    inspector = builder.inspector_column

    if previous:
        previous_state = builder.ledger_state_sql("p")
        activity_sql = (
            "CASE WHEN p._row_key IS NULL OR "
            f"({builder.ledger_change_sql('t', 'p')}) "
            "THEN 1 ELSE 0 END"
        )
        previous_unfinished_sql = (
            "CASE WHEN p._row_key IS NOT NULL "
            f"AND ({previous_state}) <> 'completed' "
            "THEN 1 ELSE 0 END"
        )
        join_sql = f"LEFT JOIN {previous} p ON p._row_key=t._row_key"
    else:
        utc_start, utc_end = await get_business_date_range_utc_bounds(
            cur,
            report_date,
            report_date,
        )
        activity_sql = (
            f"CASE WHEN {_date_activity_sql('t')} THEN 1 ELSE 0 END"
        )
        parameters.extend(
            [utc_start, utc_end, utc_start, utc_end]
        )
        previous_unfinished_sql = "0"
        join_sql = ""

    inner_sql = f"""
        SELECT
            t._row_key AS row_key,
            COALESCE(NULLIF(TRIM(t.`{community}`), ''), '未分配社区')
                AS community,
            TRIM(IFNULL(t.`{inspector}`, '')) AS inspector,
            {state_sql} AS task_state,
            {unable_sql} AS unable_to_verify,
            {reached_bottom_sql} AS reached_bottom,
            CASE WHEN {online_sql} THEN 1 ELSE 0 END AS online_present,
            {activity_sql} AS activity,
            {previous_unfinished_sql} AS previous_unfinished
        FROM {today} t
        {join_sql}
    """
    if not previous:
        derived_sql = f"""
            SELECT raw.*,
                   CASE WHEN raw.activity=0
                             AND raw.online_present=1
                             AND raw.task_state <> 'completed'
                        THEN 1 ELSE 0 END AS baseline_carryover
            FROM ({inner_sql}) raw
        """
        carry_condition = "candidate.baseline_carryover=1"
        candidate_filter = (
            "(candidate.activity=1 OR candidate.baseline_carryover=1)"
        )
    else:
        derived_sql = f"SELECT raw.*, 0 AS baseline_carryover FROM ({inner_sql}) raw"
        carry_condition = "candidate.previous_unfinished=1"
        candidate_filter = (
            "(candidate.activity=1 OR candidate.previous_unfinished=1)"
        )

    insert_sql = f"""
        INSERT INTO _daily_task_ledger (
            report_date, parser_type, row_key, source, included,
            online_present, community, inspector, task_state,
            unable_to_verify, reached_bottom
        )
        SELECT
            %s,
            %s,
            candidate.row_key,
            CASE
                WHEN candidate.online_present=0 THEN 'removed'
                WHEN {carry_condition}
                    THEN 'carryover'
                ELSE 'activity'
            END,
            CASE
                WHEN candidate.online_present=0
                     AND candidate.task_state <> 'completed'
                    THEN 0
                ELSE 1
            END,
            candidate.online_present,
            candidate.community,
            candidate.inspector,
            candidate.task_state,
            candidate.unable_to_verify,
            candidate.reached_bottom
        FROM ({derived_sql}) candidate
        WHERE {candidate_filter}
        ON DUPLICATE KEY UPDATE
            source=VALUES(source),
            included=VALUES(included),
            online_present=VALUES(online_present),
            community=VALUES(community),
            inspector=VALUES(inspector),
            task_state=VALUES(task_state),
            unable_to_verify=VALUES(unable_to_verify),
            reached_bottom=VALUES(reached_bottom),
            updated_at=CURRENT_TIMESTAMP
    """
    await cur.execute(
        insert_sql,
        (report_date, builder.parser_type, *parameters),
    )

    if previous:
        previous_state = builder.ledger_state_sql("p")
        previous_community = builder.community_column
        previous_inspector = builder.inspector_column
        await cur.execute(
            f"""
            INSERT INTO _daily_task_ledger (
                report_date, parser_type, row_key, source, included,
                online_present, community, inspector, task_state,
                unable_to_verify, reached_bottom
            )
            SELECT
                %s,
                %s,
                p._row_key,
                'removed',
                0,
                0,
                COALESCE(
                    NULLIF(TRIM(p.`{previous_community}`), ''),
                    '未分配社区'
                ),
                TRIM(IFNULL(p.`{previous_inspector}`, '')),
                {previous_state},
                {builder.ledger_unable_sql("p")},
                {builder.ledger_reached_bottom_sql("p")}
            FROM {previous} p
            LEFT JOIN {today} t ON t._row_key=p._row_key
            WHERE t._row_key IS NULL
              AND ({previous_state}) <> 'completed'
            ON DUPLICATE KEY UPDATE
                source='removed',
                included=0,
                online_present=0,
                community=VALUES(community),
                inspector=VALUES(inspector),
                task_state=VALUES(task_state),
                unable_to_verify=VALUES(unable_to_verify),
                reached_bottom=VALUES(reached_bottom),
                updated_at=CURRENT_TIMESTAMP
            """,
            (report_date, builder.parser_type),
        )

    # 如果某次移除后的下一次同步已不再包含该行，仍需关闭此前遗留的未完成流水。
    await cur.execute(
        f"""
        UPDATE _daily_task_ledger ledger
        LEFT JOIN {today} current_snapshot
          ON current_snapshot._row_key=ledger.row_key
        SET ledger.source='removed',
            ledger.included=0,
            ledger.online_present=0,
            ledger.updated_at=CURRENT_TIMESTAMP
        WHERE ledger.report_date=%s
          AND ledger.parser_type=%s
          AND ledger.included=1
          AND ledger.task_state <> 'completed'
          AND current_snapshot._row_key IS NULL
        """,
        (report_date, builder.parser_type),
    )

    await cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(included), 0) "
        "FROM _daily_task_ledger "
        "WHERE report_date=%s AND parser_type=%s",
        (report_date, builder.parser_type),
    )
    ledger_rows, included_rows = await cur.fetchone()
    await cur.execute(
        """
        INSERT INTO _daily_task_ledger_runs (
            report_date, parser_type, snapshot_table,
            previous_snapshot_table, ledger_rows, included_rows,
            generation_method
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            snapshot_table=VALUES(snapshot_table),
            previous_snapshot_table=VALUES(previous_snapshot_table),
            ledger_rows=VALUES(ledger_rows),
            included_rows=VALUES(included_rows),
            generation_method=VALUES(generation_method),
            generated_at=CURRENT_TIMESTAMP
        """,
        (
            report_date,
            builder.parser_type,
            snapshot_name,
            previous_name,
            ledger_rows,
            included_rows,
            generation_method,
        ),
    )
    return {
        "snapshot_table": snapshot_name,
        "previous_snapshot_table": previous_name,
        "ledger_rows": int(ledger_rows or 0),
        "included_rows": int(included_rows or 0),
    }


async def aggregate_ledger_into_reports(
    cur,
    builder: Any,
    report_date: str,
    inspector_table: str,
    community_table: str,
) -> dict:
    """把逐任务流水聚合到现有公开日报表。"""
    await cur.execute(f"TRUNCATE TABLE {inspector_table}")
    await cur.execute(f"TRUNCATE TABLE {community_table}")
    await cur.execute(
        f"""
        INSERT INTO {inspector_table} (
            社区, 姓名, 数据总数, 未核查, 已核查, 已完成,
            核查完成率, 无法见底数, 核查见底率
        )
        SELECT
            community,
            inspector,
            COUNT(*),
            SUM(task_state='unchecked'),
            SUM(task_state='checked'),
            SUM(task_state='completed'),
            ROUND(SUM(task_state='completed') / COUNT(*), 2),
            SUM(unable_to_verify),
            ROUND(SUM(reached_bottom) / COUNT(*), 2)
        FROM _daily_task_ledger
        WHERE report_date=%s
          AND parser_type=%s
          AND included=1
          AND inspector <> ''
          AND inspector <> '核查人'
          AND community <> ''
          AND community <> '社区'
          AND community <> '下发社区'
        GROUP BY community, inspector
        ORDER BY community, inspector
        """,
        (report_date, builder.parser_type),
    )
    await insert_zero_member_rows(cur, inspector_table, report_date)
    await rebuild_community_report_table(
        cur,
        inspector_table,
        community_table,
    )
    await cur.execute(f"SELECT COUNT(*) FROM {inspector_table}")
    inspector_rows = int((await cur.fetchone())[0])
    await cur.execute(f"SELECT COUNT(*) FROM {community_table}")
    community_rows = int((await cur.fetchone())[0])
    return {
        "inspector_rows": inspector_rows,
        "community_rows": community_rows,
    }
