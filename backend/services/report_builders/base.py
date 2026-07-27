"""日报生成器基类 - 工作量统计（对比快照检测状态变更）

工作量口径：
- 数据总数 = 当天新入库 + 当天状态变更的数据
- 新入库数据按当前状态归类：未核查、已核查或已完成
- 已有数据按当天状态变化归类：填写现住址算已核查，填写核查结果算已完成

当天没有快照时不生成日报，避免把当前全量数据写到错误的历史日期。
当天有快照、但没有前一天快照时，只统计当天实际发生变化的数据。
"""

from datetime import date, timedelta
from database import db_manager
from services.business_time import get_business_date_range_utc_bounds
from services.report_members import insert_zero_member_rows


class BaseReportBuilder:
    parser_type: str = ""
    source_table: str = ""
    table_suffix: str = ""
    see_base_keywords: list[str] = []
    result_column: str = "核查结果"

    INSPECTOR_COLS = """
        社区 VARCHAR(100) NOT NULL,
        姓名 VARCHAR(100) NOT NULL,
        数据总数 INT DEFAULT 0,
        未核查 INT DEFAULT 0,
        已核查 INT DEFAULT 0,
        已完成 INT DEFAULT 0,
        核查完成率 DECIMAL(5,2) DEFAULT 0.00,
        无法见底数 INT DEFAULT 0,
        核查见底率 DECIMAL(5,2) DEFAULT 0.00,
        PRIMARY KEY (社区, 姓名)
    """

    COMMUNITY_COLS = """
        社区 VARCHAR(100) NOT NULL PRIMARY KEY,
        数据总数 INT DEFAULT 0,
        未核查 INT DEFAULT 0,
        已核查 INT DEFAULT 0,
        已完成 INT DEFAULT 0,
        核查完成率 DECIMAL(5,2) DEFAULT 0.00,
        无法见底数 INT DEFAULT 0,
        核查见底率 DECIMAL(5,2) DEFAULT 0.00
    """

    async def build(self, date_str: str) -> dict:
        """生成工作量日报（对比前一天和当天快照检测状态变更）"""
        t_inspector = f"`{date_str}_daily_{self.table_suffix}_inspector`"
        t_community = f"`{date_str}_daily_{self.table_suffix}_community`"
        today_snap = f"`{date_str}_snapshot_{self.table_suffix}`"

        d = date.fromisoformat(date_str)
        prev_date = (d - timedelta(days=1)).isoformat()
        prev_snap = f"`{prev_date}_snapshot_{self.table_suffix}`"

        pool = db_manager.get_pool("daily_report")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                # 检查当天快照是否存在
                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta WHERE table_name = %s",
                    (f"{date_str}_snapshot_{self.table_suffix}",),
                )
                has_today = await cur.fetchone() is not None
                if not has_today:
                    return {
                        "implemented": False,
                        "message": f"{date_str} 没有同步快照，不能生成日报",
                    }

                # 检查前一天快照是否存在
                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta WHERE table_name = %s",
                    (f"{prev_date}_snapshot_{self.table_suffix}",),
                )
                has_prev = await cur.fetchone() is not None

                await cur.execute(f"CREATE TABLE IF NOT EXISTS {t_inspector} ({self.INSPECTOR_COLS}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
                await cur.execute(f"CREATE TABLE IF NOT EXISTS {t_community} ({self.COMMUNITY_COLS}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
                await cur.execute(f"TRUNCATE TABLE {t_inspector}")
                await cur.execute(f"TRUNCATE TABLE {t_community}")

                if has_prev:
                    # 工作量统计：对比快照检测状态变更
                    insp_sql, comm_sql = self._build_workload_sql(today_snap, prev_snap)
                    sql_params = None
                    print(f"[BUILD] {self.parser_type} {date_str}: 工作量统计（对比 {prev_date} 快照）")
                else:
                    # 无前一天快照：用 _last_updated_at 筛选当天有活动的数据
                    insp_sql, comm_sql = self._build_workload_sql(today_snap, None)
                    sql_params = await get_business_date_range_utc_bounds(
                        cur, date_str, date_str
                    )
                    print(f"[BUILD] {self.parser_type} {date_str}: 首日统计（用 _last_updated_at 筛选当天活动）")

                if sql_params:
                    await cur.execute(f"INSERT INTO {t_inspector} (社区, 姓名, 数据总数, 未核查, 已核查, 已完成, 核查完成率, 无法见底数, 核查见底率) {insp_sql}", sql_params)
                    await cur.execute(f"INSERT INTO {t_community} (社区, 数据总数, 未核查, 已核查, 已完成, 核查完成率, 无法见底数, 核查见底率) {comm_sql}", sql_params)
                else:
                    await cur.execute(f"INSERT INTO {t_inspector} (社区, 姓名, 数据总数, 未核查, 已核查, 已完成, 核查完成率, 无法见底数, 核查见底率) {insp_sql}")
                    await cur.execute(f"INSERT INTO {t_community} (社区, 数据总数, 未核查, 已核查, 已完成, 核查完成率, 无法见底数, 核查见底率) {comm_sql}")

                await insert_zero_member_rows(cur, t_inspector, date_str)

                await cur.execute(f"SELECT COUNT(*) FROM {t_inspector}")
                insp_count = (await cur.fetchone())[0]
                await cur.execute(f"SELECT COUNT(*) FROM {t_community}")
                comm_count = (await cur.fetchone())[0]

                for tname in [f"{date_str}_daily_{self.table_suffix}_inspector", f"{date_str}_daily_{self.table_suffix}_community"]:
                    await cur.execute(
                        "INSERT INTO _daily_report_meta (table_name, report_date, parser_type, generation_method) "
                        "VALUES (%s, %s, %s, 'workload') ON DUPLICATE KEY UPDATE generated_at = NOW()",
                        (tname, date_str, self.parser_type),
                    )

            return {"date": date_str, "type": self.parser_type, "inspector_rows": insp_count, "community_rows": comm_count}
        finally:
            pool.release(conn)

    def _build_workload_sql(self, today: str, prev: str | None) -> tuple[str, str]:
        """生成工作量统计 SQL（对比快照检测状态变更）

        today: 当天快照表名
        prev: 前一天快照表名（None 时当天数据全算新增）
        """
        rc = self.result_column
        see_base = " OR ".join(f"t.{rc} LIKE '%%{kw}%%'" for kw in self.see_base_keywords)
        no_base = f"t.{rc} LIKE '%%无法核实%%'"

        if prev:
            join_clause = f"LEFT JOIN {prev} prev ON t._row_key = prev._row_key"
            # 只统计有变更的数据：新增 或 现住址变更 或 核查结果变更
            change_filter = """(
                prev._row_key IS NULL
                OR IFNULL(prev.现住址, '') <> IFNULL(t.现住址, '')
                OR IFNULL(prev.{rc}, '') <> IFNULL(t.{rc}, '')
            )""".format(rc=rc)
            # 新增数据可能在首次入库时就已经填写了现住址或核查结果，
            # 必须按当前状态归类，不能只把“空地址”算作未核查。
            cond_unchecked = """prev._row_key IS NULL
                AND IFNULL(t.现住址, '') = ''
                AND IFNULL(t.{rc}, '') = ''""".format(rc=rc)
            cond_checked = """(
                    prev._row_key IS NULL
                    AND IFNULL(t.现住址, '') <> ''
                    AND IFNULL(t.{rc}, '') = ''
                ) OR (
                    prev._row_key IS NOT NULL
                    AND IFNULL(prev.现住址, '') = ''
                    AND IFNULL(t.现住址, '') <> ''
                    AND IFNULL(t.{rc}, '') = ''
                )""".format(rc=rc)
            cond_done = """(
                    prev._row_key IS NULL
                    AND IFNULL(t.{rc}, '') <> ''
                ) OR (
                    prev._row_key IS NOT NULL
                    AND IFNULL(prev.{rc}, '') = ''
                    AND IFNULL(t.{rc}, '') <> ''
                )""".format(rc=rc)
        else:
            # 无前一天快照：用 _last_updated_at 筛选当天有活动的数据，按当前状态分类
            join_clause = ""
            change_filter = (
                "t._last_updated_at >= %s AND t._last_updated_at < %s"
            )
            cond_unchecked = "(t.现住址 IS NULL OR t.现住址 = '')"
            cond_checked = f"t.现住址 <> '' AND (t.{rc} IS NULL OR t.{rc} = '')"
            cond_done = f"t.{rc} <> ''"

        inspector_sql = f"""
            SELECT
                t.社区, t.核查人, COUNT(*),
                SUM(CASE WHEN {cond_unchecked} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {cond_checked} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {cond_done} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {cond_done} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END,
                SUM(CASE WHEN {no_base} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {see_base} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END
            FROM {today} t
            {join_clause}
            WHERE t.核查人 IS NOT NULL AND t.核查人 <> '' AND t.核查人 <> '核查人'
              AND t.社区 IS NOT NULL AND t.社区 <> '' AND t.社区 <> '社区'
              AND {change_filter}
            GROUP BY t.社区, t.核查人
            ORDER BY t.社区, t.核查人
        """

        community_sql = f"""
            SELECT
                t.社区, COUNT(*),
                SUM(CASE WHEN {cond_unchecked} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {cond_checked} THEN 1 ELSE 0 END),
                SUM(CASE WHEN {cond_done} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {cond_done} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END,
                SUM(CASE WHEN {no_base} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {see_base} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END
            FROM {today} t
            {join_clause}
            WHERE t.社区 IS NOT NULL AND t.社区 <> '' AND t.社区 <> '社区'
              AND {change_filter}
            GROUP BY t.社区
            ORDER BY t.社区
        """

        return inspector_sql, community_sql

    def build_stats_sql(self, src: str) -> tuple[str, str]:
        """生成全量统计 SQL（用于从指定快照子查询统计存量）

        列名加 AS 别名，便于在子查询中引用
        """
        rc = self.result_column
        see_base = " OR ".join(f"t.{rc} LIKE '%%{kw}%%'" for kw in self.see_base_keywords)
        no_base = f"t.{rc} LIKE '%%无法核实%%'"

        inspector_sql = f"""
            SELECT
                t.社区 AS 社区, t.核查人 AS 姓名, COUNT(*) AS 数据总数,
                SUM(CASE WHEN t.现住址 IS NULL OR t.现住址 = '' THEN 1 ELSE 0 END) AS 未核查,
                SUM(CASE WHEN t.现住址 <> '' AND (t.{rc} IS NULL OR t.{rc} = '') THEN 1 ELSE 0 END) AS 已核查,
                SUM(CASE WHEN t.现住址 <> '' AND t.{rc} <> '' THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN t.现住址 <> '' AND t.{rc} <> '' THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查完成率,
                SUM(CASE WHEN {no_base} THEN 1 ELSE 0 END) AS 无法见底数,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {see_base} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查见底率
            FROM {src} t
            WHERE t.核查人 IS NOT NULL AND t.核查人 <> '' AND t.核查人 <> '核查人'
              AND t.社区 IS NOT NULL AND t.社区 <> '' AND t.社区 <> '社区'
            GROUP BY t.社区, t.核查人
            ORDER BY t.社区, t.核查人
        """

        community_sql = f"""
            SELECT
                t.社区 AS 社区, COUNT(*) AS 数据总数,
                SUM(CASE WHEN t.现住址 IS NULL OR t.现住址 = '' THEN 1 ELSE 0 END) AS 未核查,
                SUM(CASE WHEN t.现住址 <> '' AND (t.{rc} IS NULL OR t.{rc} = '') THEN 1 ELSE 0 END) AS 已核查,
                SUM(CASE WHEN t.现住址 <> '' AND t.{rc} <> '' THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN t.现住址 <> '' AND t.{rc} <> '' THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查完成率,
                SUM(CASE WHEN {no_base} THEN 1 ELSE 0 END) AS 无法见底数,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {see_base} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查见底率
            FROM {src} t
            WHERE t.社区 IS NOT NULL AND t.社区 <> '' AND t.社区 <> '社区'
            GROUP BY t.社区
            ORDER BY t.社区
        """

        return inspector_sql, community_sql
