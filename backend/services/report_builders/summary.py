"""总汇总表生成器 - 合并分表的社区汇总 + 网格员人数

支持配置使用哪些分表类型（_system_config.summary_types）
"""

import json
from database import db_manager
from services.grid_member_status import active_member_sql
from services.report_builders import BUILDERS

SUMMARY_COLS = """
    社区 VARCHAR(100) NOT NULL PRIMARY KEY,
    数据总数 INT DEFAULT 0,
    未核查 INT DEFAULT 0,
    已核查 INT DEFAULT 0,
    已完成 INT DEFAULT 0,
    核查完成率 DECIMAL(5,2) DEFAULT 0.00,
    无法见底数 INT DEFAULT 0,
    核查见底率 DECIMAL(5,2) DEFAULT 0.00,
    网格员人数 INT DEFAULT 0,
    当日人均核查数 DECIMAL(8,2) DEFAULT 0.00
"""

SUMMARY_OUTPUT_COLS = [
    "社区",
    "数据总数",
    "未核查",
    "已核查",
    "已完成",
    "核查完成率",
    "无法见底数",
    "核查见底率",
    "网格员人数",
    "当日人均核查数",
]


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


async def build_summary(date_str: str) -> dict:
    """生成总汇总表（合并分表社区汇总 + 网格员人数）"""
    t_summary = f"`{date_str}_daily_summary`"
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            # 读取配置：使用哪些分表类型
            summary_types = await _get_summary_types(cur)

            # 检查已配置分表的社区汇总日报表是否存在
            existing_tables = []
            for ptype in summary_types:
                builder = BUILDERS.get(ptype)
                if not builder:
                    continue
                tname = f"{date_str}_daily_{builder.table_suffix}_community"
                snapshot_name = f"{date_str}_snapshot_{builder.table_suffix}"
                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta "
                    "WHERE table_name IN (%s, %s)",
                    (tname, snapshot_name),
                )
                available = {row[0] for row in await cur.fetchall()}
                if {tname, snapshot_name}.issubset(available):
                    existing_tables.append(f"{builder.table_suffix}_community")

            if not existing_tables:
                return {
                    "implemented": False,
                    "message": f"{date_str} 没有可用快照和分汇总表，不能生成总汇总表",
                }

            await cur.execute(
                f"CREATE TABLE IF NOT EXISTS {t_summary} ({SUMMARY_COLS}) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
            await cur.execute(f"TRUNCATE TABLE {t_summary}")

            union_parts = []
            for suffix in existing_tables:
                union_parts.append(
                    f"SELECT 社区, 数据总数, 未核查, 已核查, 已完成, 无法见底数 "
                    f"FROM `{date_str}_daily_{suffix}`"
                )
            union_sql = " UNION ALL ".join(union_parts)
            active_condition = active_member_sql()

            await cur.execute(f"""
                INSERT INTO {t_summary} (社区, 数据总数, 未核查, 已核查, 已完成, 无法见底数, 网格员人数)
                SELECT
                    t.社区,
                    SUM(t.数据总数),
                    SUM(t.未核查),
                    SUM(t.已核查),
                    SUM(t.已完成),
                    SUM(t.无法见底数),
                    COALESCE((
                        SELECT COUNT(*) FROM OnlineData._grid_members
                        WHERE community = t.社区 AND {active_condition}
                    ), 0)
                FROM ({union_sql}) t
                GROUP BY t.社区
            """, (date_str,))

            await cur.execute(f"""
                UPDATE {t_summary} SET
                    核查完成率 = CASE WHEN 数据总数 > 0 THEN ROUND(已完成 / 数据总数, 2) ELSE 0 END,
                    核查见底率 = CASE WHEN 数据总数 > 0 THEN ROUND((数据总数 - 无法见底数) / 数据总数, 2) ELSE 0 END,
                    当日人均核查数 = CASE WHEN 网格员人数 > 0 THEN ROUND(已完成 / 网格员人数, 2) ELSE 0 END
            """)

            await cur.execute(
                "INSERT INTO _daily_report_meta (table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, '总汇总表', 'manual') ON DUPLICATE KEY UPDATE generated_at = NOW()",
                (f"{date_str}_daily_summary", date_str),
            )

            await cur.execute(f"SELECT COUNT(*) FROM {t_summary}")
            count = (await cur.fetchone())[0]

        return {"date": date_str, "type": "总汇总表", "rows": count, "implemented": True}
    finally:
        pool.release(conn)


async def get_summary(date_str: str) -> dict:
    """查看总汇总表"""
    t_summary = f"`{date_str}_daily_summary`"
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT table_name FROM _daily_report_meta "
                "WHERE report_date = %s AND RIGHT(parser_type, 9) = '_snapshot' "
                "LIMIT 1",
                (date_str,),
            )
            if not await cur.fetchone():
                return {
                    "exists": False,
                    "message": f"{date_str} 没有同步快照，暂无总汇总表",
                }

            await cur.execute(
                "SELECT table_name FROM _daily_report_meta WHERE table_name = %s",
                (f"{date_str}_daily_summary",),
            )
            if not await cur.fetchone():
                return {
                    "exists": False,
                    "message": f"{date_str} 尚未生成总汇总表",
                }

            active_condition = active_member_sql()
            await cur.execute(f"""
                SELECT
                    s.社区,
                    s.数据总数,
                    s.未核查,
                    s.已核查,
                    s.已完成,
                    s.核查完成率,
                    s.无法见底数,
                    s.核查见底率,
                    COALESCE((
                        SELECT COUNT(*) FROM OnlineData._grid_members
                        WHERE community = s.社区 AND {active_condition}
                    ), 0),
                    CASE
                        WHEN COALESCE((
                            SELECT COUNT(*) FROM OnlineData._grid_members
                            WHERE community = s.社区 AND {active_condition}
                        ), 0) > 0
                        THEN ROUND(s.已完成 / (
                            SELECT COUNT(*) FROM OnlineData._grid_members
                            WHERE community = s.社区 AND {active_condition}
                        ), 2)
                        ELSE 0
                    END
                FROM {t_summary} s
                ORDER BY s.社区
            """, (date_str, date_str, date_str))
            rows = await cur.fetchall()

        return {
            "exists": True,
            "columns": SUMMARY_OUTPUT_COLS,
            "data": [dict(zip(SUMMARY_OUTPUT_COLS, row)) for row in rows],
        }
    finally:
        pool.release(conn)
