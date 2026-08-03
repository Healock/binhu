"""总汇总表生成器 - 合并分表的社区汇总 + 网格员人数

支持配置使用哪些分表类型（_system_config.summary_types）
"""

import json
from database import db_manager
from services.grid_member_status import active_member_sql
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
from services.report_attendance import load_community_person_days

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
    "在岗人日",
    "每日人均核查数",
]

SUMMARY_INSPECTOR_OUTPUT_COLS = [
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


async def _load_summary_types() -> list[str]:
    """读取当前参与总汇总表的分表类型，并及时释放数据库连接。"""
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            return await _get_summary_types(cur)
    finally:
        pool.release(conn)


async def build_summary_with_subreports(date_str: str) -> dict:
    """先重建已配置的分汇总表，再生成总汇总表。"""
    summary_types = await _load_summary_types()
    subreports = []

    for parser_type in summary_types:
        builder = BUILDERS[parser_type]
        try:
            result = await builder.build(date_str)
        except Exception:
            import traceback

            traceback.print_exc()
            return {
                "implemented": False,
                "failed_type": parser_type,
                "subreports": subreports,
                "message": (
                    f"无法生成总汇总表：「{parser_type}」分汇总表生成失败，"
                    "请查看服务器日志"
                ),
            }

        if result.get("implemented") is False:
            reason = result.get("message") or "没有可用的同步快照"
            return {
                "implemented": False,
                "failed_type": parser_type,
                "subreports": subreports,
                "message": (
                    f"无法生成总汇总表：「{parser_type}」分汇总表未生成。"
                    f"{reason}"
                ),
            }

        subreports.append({"parser_type": parser_type, **result})

    summary_result = await build_summary(date_str, summary_types=summary_types)
    return {**summary_result, "subreports": subreports}


async def build_summary(
    date_str: str,
    summary_types: list[str] | None = None,
) -> dict:
    """生成总汇总表（合并分表社区汇总 + 网格员人数）"""
    t_summary = f"`{date_str}_daily_summary`"
    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            # 未指定时读取配置；手动串联生成时沿用开始时读取的同一份配置。
            if summary_types is None:
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
                    report_rows.社区,
                    report_rows.数据总数,
                    report_rows.未核查,
                    report_rows.已核查,
                    report_rows.已完成,
                    report_rows.无法见底数,
                    COALESCE(member_counts.网格员人数, 0)
                FROM (
                    SELECT
                        COALESCE(formal_community.name, t.社区) AS 社区,
                        SUM(t.数据总数) AS 数据总数,
                        SUM(t.未核查) AS 未核查,
                        SUM(t.已核查) AS 已核查,
                        SUM(t.已完成) AS 已完成,
                        SUM(t.无法见底数) AS 无法见底数
                    FROM ({union_sql}) t
                    LEFT JOIN OnlineData._community_aliases
                        AS community_alias
                      ON community_alias.alias = t.社区
                    LEFT JOIN OnlineData._communities
                        AS formal_community
                      ON formal_community.id = community_alias.community_id
                    GROUP BY COALESCE(formal_community.name, t.社区)
                ) AS report_rows
                LEFT JOIN (
                    SELECT community.name AS community,
                           COUNT(DISTINCT member.id) AS 网格员人数
                    FROM OnlineData._grid_members AS member
                    JOIN OnlineData._grid_member_department_links AS link
                      ON link.member_id=member.id
                    JOIN OnlineData._departments AS department
                      ON department.id=link.department_id
                    JOIN OnlineData._communities AS community
                      ON community.id=department.community_id
                    WHERE member.position IN ('组长', '组员')
                      AND {active_condition}
                    GROUP BY community.name
                ) AS member_counts
                  ON member_counts.community = report_rows.社区
            """, (date_str,))

            await cur.execute(f"""
                UPDATE {t_summary} SET
                    核查完成率 = CASE WHEN 数据总数 > 0 THEN ROUND(已完成 / 数据总数, 2) ELSE 0 END,
                    核查见底率 = CASE WHEN 已完成 > 0 THEN ROUND(GREATEST(已完成 - 无法见底数, 0) / 已完成, 2) ELSE 0 END,
                    当日人均核查数 = CASE WHEN 网格员人数 > 0 THEN ROUND(已完成 / 网格员人数, 2) ELSE 0 END
            """)

            await cur.execute(
                "INSERT INTO _daily_report_meta (table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, '总汇总表', 'sync') ON DUPLICATE KEY UPDATE "
                "generation_method='sync', generated_at=NOW()",
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

            summary_types = await _get_summary_types(cur)
            alias_lookup = await get_community_alias_lookup(cur)
            all_inspector_rows = []
            for parser_type in summary_types:
                builder = BUILDERS.get(parser_type)
                if not builder:
                    continue
                inspector_name = (
                    f"{date_str}_daily_{builder.table_suffix}_inspector"
                )
                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta "
                    "WHERE table_name=%s",
                    (inspector_name,),
                )
                if not await cur.fetchone():
                    continue
                inspector_table = f"`{inspector_name}`"
                await cur.execute(
                    f"SELECT * FROM {inspector_table} ORDER BY 社区, 姓名"
                )
                raw_inspector_rows = canonicalize_inspector_rows(
                    await cur.fetchall(),
                    alias_lookup,
                )
                inspector_rows = await complete_inspector_rows(
                    cur,
                    raw_inspector_rows,
                    date_str,
                )
                all_inspector_rows.extend(
                    canonicalize_inspector_rows(
                        inspector_rows,
                        alias_lookup,
                    )
                )

            if not all_inspector_rows:
                return {
                    "exists": False,
                    "message": f"{date_str} 没有可用的分汇总表",
                }

            active_members = [
                (
                    canonical_community(community, alias_lookup),
                    name,
                )
                for community, name in await get_active_members(
                    cur,
                    date_str,
                )
            ]
            inspector_rows = merge_inspector_rows(
                all_inspector_rows,
                active_members,
            )
            community_rows = aggregate_community_rows(inspector_rows)
            member_counts: dict[str, int] = {}
            for community, _ in active_members:
                formal_community = canonical_community(
                    community,
                    alias_lookup,
                )
                member_counts[formal_community] = (
                    member_counts.get(formal_community, 0) + 1
                )
            community_person_days, attendance = (
                await load_community_person_days(
                    {date_str},
                    alias_lookup,
                )
            )
            community_rows = [
                (
                    *row,
                    member_counts.get(str(row[0]), 0),
                    (
                        community_person_days.get(str(row[0]), 0)
                        if attendance["complete"]
                        else None
                    ),
                    (
                        calculate_ratio(
                            int(row[4] or 0),
                            community_person_days.get(str(row[0]), 0),
                        )
                        if attendance["complete"]
                        else None
                    ),
                )
                for row in community_rows
            ]

        inspector_table = {
            "columns": SUMMARY_INSPECTOR_OUTPUT_COLS,
            "data": [
                dict(zip(SUMMARY_INSPECTOR_OUTPUT_COLS, row))
                for row in inspector_rows
            ],
        }
        community_table = {
            "columns": SUMMARY_OUTPUT_COLS,
            "data": [
                dict(zip(SUMMARY_OUTPUT_COLS, row))
                for row in community_rows
            ],
        }
        return {
            "exists": True,
            # 保留原来的顶层社区表字段，兼容尚未升级的调用方。
            **community_table,
            "attendance": attendance,
            "inspector": inspector_table,
            "community": community_table,
        }
    finally:
        pool.release(conn)
