"""时间区间聚合查询 - 从快照去重取最新状态（存量口径）

单日日报是工作量统计（流量），区间查询是存量统计——两者口径不同：
- 单日日报：当天新入库+当天状态变更的数据（流量）
- 区间查询：区间结束时所有数据的最终状态（存量，去重不翻倍）

支持总汇总表类型配置（_system_config.summary_types）
"""

import json
from database import db_manager
from services.business_time import get_business_date_range_utc_bounds
from services.report_builders import BUILDERS
from services.report_members import (
    aggregate_community_rows,
    calculate_ratio,
    complete_inspector_rows,
    get_active_members,
    merge_inspector_rows,
)


async def _get_summary_types(cur) -> list[str]:
    """从 _system_config 读取总汇总表使用的分表类型，默认全部"""
    await cur.execute("SELECT config_value FROM OnlineData._system_config WHERE config_key = 'summary_types'")
    row = await cur.fetchone()
    if not row or not row[0]:
        return list(BUILDERS.keys())
    try:
        types = json.loads(row[0])
        return [t for t in types if t in BUILDERS] or list(BUILDERS.keys())
    except Exception:
        return list(BUILDERS.keys())


async def _find_snapshots(cur, start_date: str, end_date: str, parser_type: str) -> list[str]:
    """从 _daily_report_meta 查出区间内的快照表名"""
    await cur.execute(
        "SELECT table_name FROM _daily_report_meta "
        "WHERE report_date BETWEEN %s AND %s AND parser_type = %s "
        "ORDER BY report_date",
        (start_date, end_date, f"{parser_type}_snapshot"),
    )
    seen = set()
    result = []
    for r in await cur.fetchall():
        if r[0] not in seen:
            seen.add(r[0])
            result.append(r[0])
    return result


def _build_dedup_subquery(snapshot_tables: list[str]) -> str:
    """构建 UNION ALL + ROW_NUMBER 去重子查询"""
    union_sql = " UNION ALL ".join(
        f"SELECT * FROM `{tn}`" for tn in snapshot_tables
    )
    return f"""
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY _row_key ORDER BY _last_updated_at DESC) AS _rn
            FROM ({union_sql}) _all_snap
            WHERE _first_seen_at >= %s AND _first_seen_at < %s
        ) _dedup
        WHERE _rn = 1
    """


async def get_report_range(start_date: str, end_date: str, parser_type: str) -> dict:
    """区间查询分汇总表（存量口径：从快照去重取最新状态 + 全量统计）"""
    if parser_type not in BUILDERS:
        return {"exists": False, "message": f"未实现的类型：{parser_type}"}

    builder = BUILDERS[parser_type]
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            snapshots = await _find_snapshots(cur, start_date, end_date, parser_type)

            if not snapshots:
                return {
                    "exists": False,
                    "message": f"{start_date} 至 {end_date} 没有同步快照，暂无统计数据",
                }

            utc_bounds = await get_business_date_range_utc_bounds(
                cur, start_date, end_date
            )
            dedup_subquery = _build_dedup_subquery(snapshots)
            src = f"({dedup_subquery})"
            insp_sql, _ = builder.build_stats_sql(src)
            await cur.execute(insp_sql, utc_bounds)
            insp_rows = await cur.fetchall()
            insp_rows = await complete_inspector_rows(cur, insp_rows, end_date)
            comm_rows = aggregate_community_rows(insp_rows)

        insp_cols = ["社区", "姓名", "数据总数", "未核查", "已核查", "已完成", "核查完成率", "无法见底数", "核查见底率"]
        comm_cols = ["社区", "数据总数", "未核查", "已核查", "已完成", "核查完成率", "无法见底数", "核查见底率"]

        return {
            "exists": True,
            "range": {"start": start_date, "end": end_date, "days": len(snapshots)},
            "inspector": {"columns": insp_cols, "data": [dict(zip(insp_cols, r)) for r in insp_rows]},
            "community": {"columns": comm_cols, "data": [dict(zip(comm_cols, r)) for r in comm_rows]},
        }
    finally:
        pool.release(conn)


async def get_summary_range(start_date: str, end_date: str) -> dict:
    """区间查询总汇总表（存量口径：各分表快照去重 + 全量统计 + 合并 + 网格员人数）"""
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            # 读取配置：总汇总表使用哪些分表
            summary_types = await _get_summary_types(cur)
            utc_bounds = await get_business_date_range_utc_bounds(
                cur, start_date, end_date
            )

            all_inspector_rows = []
            total_days = 0
            for ptype in summary_types:
                builder = BUILDERS.get(ptype)
                if not builder:
                    continue
                snapshots = await _find_snapshots(cur, start_date, end_date, ptype)
                if not snapshots:
                    continue
                total_days = max(total_days, len(snapshots))
                dedup_subquery = _build_dedup_subquery(snapshots)
                src = f"({dedup_subquery})"
                insp_sql, _ = builder.build_stats_sql(src)
                await cur.execute(insp_sql, utc_bounds)
                insp_rows = await cur.fetchall()
                insp_rows = await complete_inspector_rows(
                    cur,
                    insp_rows,
                    end_date,
                )
                all_inspector_rows.extend(insp_rows)

            if not all_inspector_rows:
                return {"exists": False}

            active_members = await get_active_members(cur, end_date)
            inspector_rows = merge_inspector_rows(
                all_inspector_rows,
                active_members,
            )
            merged_rows = aggregate_community_rows(inspector_rows)
            member_counts: dict[str, int] = {}
            for community, _ in active_members:
                member_counts[community] = member_counts.get(community, 0) + 1
            community_rows = []
            for row in merged_rows:
                community = str(row[0])
                count = member_counts.get(community, 0)
                completed = int(row[4] or 0)
                community_rows.append(
                    (
                        *row,
                        count,
                        calculate_ratio(completed, count),
                    )
                )
            inspector_cols = [
                "社区", "姓名", "数据总数", "未核查", "已核查", "已完成",
                "核查完成率", "无法见底数", "核查见底率",
            ]
            community_cols = [
                "社区", "数据总数", "未核查", "已核查", "已完成", "核查完成率",
                "无法见底数", "核查见底率", "网格员人数", "当日人均核查数",
            ]

        inspector_table = {
            "columns": inspector_cols,
            "data": [
                dict(zip(inspector_cols, row))
                for row in inspector_rows
            ],
        }
        community_table = {
            "columns": community_cols,
            "data": [
                dict(zip(community_cols, row))
                for row in community_rows
            ],
        }
        return {
            "exists": True,
            "range": {"start": start_date, "end": end_date, "days": total_days or 1},
            # 顶层继续提供社区表，兼容旧调用方。
            **community_table,
            "inspector": inspector_table,
            "community": community_table,
        }
    finally:
        pool.release(conn)
