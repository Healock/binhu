"""时间区间聚合查询 - 从快照去重取最新状态（存量口径）

单日日报是工作量统计（流量），区间查询是存量统计——两者口径不同：
- 单日日报：当天新入库+当天状态变更的数据（流量）
- 区间查询：区间结束时所有数据的最终状态（存量，去重不翻倍）

支持总汇总表类型配置（_system_config.summary_types）
"""

import json
from database import db_manager
from services.business_time import get_business_date_range_utc_bounds
from services.grid_member_status import active_member_sql
from services.report_builders import BUILDERS


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
            insp_sql, comm_sql = builder.build_stats_sql(src)
            await cur.execute(insp_sql, utc_bounds)
            insp_rows = await cur.fetchall()
            await cur.execute(comm_sql, utc_bounds)
            comm_rows = await cur.fetchall()

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

            union_parts = []
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
                # 用 build_stats_sql 做全量统计，取社区汇总
                _, comm_sql = builder.build_stats_sql(src)
                # comm_sql 返回带别名的列，用子查询包装取需要的列
                union_parts.append(
                    f"SELECT 社区, 数据总数, 未核查, 已核查, 已完成, 无法见底数 "
                    f"FROM ({comm_sql}) _c{builder.table_suffix}"
                )

            if not union_parts:
                return {"exists": False}

            union_sql = " UNION ALL ".join(union_parts)
            # 每个 dedup_subquery 有2个 %s（UTC 起止时间），需要传对应数量参数
            param_count = union_sql.count("%s")
            params = list(utc_bounds) * (param_count // 2)
            active_condition = active_member_sql()

            await cur.execute(f"""
                SELECT
                    t.社区,
                    SUM(t.数据总数),
                    SUM(t.未核查),
                    SUM(t.已核查),
                    SUM(t.已完成),
                    CASE WHEN SUM(t.数据总数) > 0
                         THEN ROUND(SUM(t.已完成) / SUM(t.数据总数), 2) ELSE 0 END,
                    SUM(t.无法见底数),
                    CASE WHEN SUM(t.数据总数) > 0
                         THEN ROUND((SUM(t.数据总数) - SUM(t.无法见底数)) / SUM(t.数据总数), 2)
                         ELSE 0 END,
                    COALESCE((
                        SELECT COUNT(*) FROM OnlineData._grid_members
                        WHERE community = t.社区 AND {active_condition}
                    ), 0),
                    CASE
                        WHEN COALESCE((SELECT COUNT(*) FROM OnlineData._grid_members
                                       WHERE community = t.社区 AND {active_condition}), 0) > 0
                        THEN ROUND(SUM(t.已完成) / (
                            SELECT COUNT(*) FROM OnlineData._grid_members
                            WHERE community = t.社区 AND {active_condition}
                        ), 2)
                        ELSE 0
                    END
                FROM ({union_sql}) t
                GROUP BY t.社区
                ORDER BY t.社区
            """, [end_date, end_date, end_date, *params])
            rows = await cur.fetchall()
            cols = [
                "社区", "数据总数", "未核查", "已核查", "已完成", "核查完成率",
                "无法见底数", "核查见底率", "网格员人数", "当日人均核查数",
            ]

        return {
            "exists": True,
            "range": {"start": start_date, "end": end_date, "days": total_days or 1},
            "columns": cols,
            "data": [dict(zip(cols, r)) for r in rows],
        }
    finally:
        pool.release(conn)
