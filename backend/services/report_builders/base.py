"""在线数据日报生成器基类。

单日数据由逐任务流水聚合：前期未完成且仍在线的任务会结转，当天新增或
地址、核查结果发生变化的任务会作为当天活动纳入。同一任务当天最多一条。
"""

from database import db_manager


class BaseReportBuilder:
    parser_type: str = ""
    source_table: str = ""
    table_suffix: str = ""
    community_column: str = "社区"
    inspector_column: str = "核查人"
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

    def ledger_state_sql(self, alias: str) -> str:
        result = f"IFNULL({alias}.`{self.result_column}`, '')"
        address = f"IFNULL({alias}.`现住址`, '')"
        return (
            f"CASE WHEN {result} <> '' THEN 'completed' "
            f"WHEN {address} <> '' THEN 'checked' "
            "ELSE 'unchecked' END"
        )

    def ledger_change_sql(self, today_alias: str, previous_alias: str) -> str:
        return (
            f"IFNULL({previous_alias}.`现住址`, '') "
            f"<> IFNULL({today_alias}.`现住址`, '') "
            f"OR IFNULL({previous_alias}.`{self.result_column}`, '') "
            f"<> IFNULL({today_alias}.`{self.result_column}`, '')"
        )

    def ledger_unable_sql(self, alias: str) -> str:
        return (
            f"CASE WHEN IFNULL({alias}.`{self.result_column}`, '') "
            "LIKE '%无法核实%' THEN 1 ELSE 0 END"
        )

    def ledger_reached_bottom_sql(self, alias: str) -> str:
        conditions = " OR ".join(
            f"IFNULL({alias}.`{self.result_column}`, '') "
            f"LIKE '%{keyword}%'"
            for keyword in self.see_base_keywords
        )
        if not conditions:
            return "0"
        return f"CASE WHEN {conditions} THEN 1 ELSE 0 END"

    async def build(self, date_str: str) -> dict:
        """生成包含跨日结转的当天任务日报。"""
        t_inspector = f"`{date_str}_daily_{self.table_suffix}_inspector`"
        t_community = f"`{date_str}_daily_{self.table_suffix}_community`"

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

                await cur.execute(f"CREATE TABLE IF NOT EXISTS {t_inspector} ({self.INSPECTOR_COLS}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
                await cur.execute(f"CREATE TABLE IF NOT EXISTS {t_community} ({self.COMMUNITY_COLS}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
                from services.report_ledger import (
                    aggregate_ledger_into_reports,
                    refresh_daily_ledger,
                )

                ledger_result = await refresh_daily_ledger(
                    cur,
                    self,
                    date_str,
                    generation_method="sync",
                )
                await aggregate_ledger_into_reports(
                    cur,
                    self,
                    date_str,
                    t_inspector,
                    t_community,
                )

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

            return {
                "date": date_str,
                "type": self.parser_type,
                "inspector_rows": insp_count,
                "community_rows": comm_count,
                "ledger_rows": ledger_result["ledger_rows"],
                "included_rows": ledger_result["included_rows"],
            }
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
            # 只用前一天快照判断“今天是否发生变化”；一旦进入当天工作量，
            # 必须按今天的最终状态归类。否则地址从一个非空值改成另一个
            # 非空值时会进入数据总数，却不会进入任何状态列。
            cond_unchecked = """IFNULL(t.现住址, '') = ''
                AND IFNULL(t.{rc}, '') = ''""".format(rc=rc)
            cond_checked = """IFNULL(t.现住址, '') <> ''
                AND IFNULL(t.{rc}, '') = ''""".format(rc=rc)
            cond_done = "IFNULL(t.{rc}, '') <> ''".format(rc=rc)
        else:
            # 无前一天快照：用 _last_updated_at 筛选当天有活动的数据，按当前状态分类
            join_clause = ""
            change_filter = (
                "t._last_updated_at >= %s AND t._last_updated_at < %s"
            )
            cond_unchecked = (
                f"IFNULL(t.现住址, '') = '' AND IFNULL(t.{rc}, '') = ''"
            )
            cond_checked = (
                f"IFNULL(t.现住址, '') <> '' AND IFNULL(t.{rc}, '') = ''"
            )
            cond_done = f"IFNULL(t.{rc}, '') <> ''"

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
                SUM(CASE WHEN IFNULL(t.现住址, '') = '' AND IFNULL(t.{rc}, '') = '' THEN 1 ELSE 0 END) AS 未核查,
                SUM(CASE WHEN IFNULL(t.现住址, '') <> '' AND IFNULL(t.{rc}, '') = '' THEN 1 ELSE 0 END) AS 已核查,
                SUM(CASE WHEN IFNULL(t.{rc}, '') <> '' THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN IFNULL(t.{rc}, '') <> '' THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查完成率,
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
                SUM(CASE WHEN IFNULL(t.现住址, '') = '' AND IFNULL(t.{rc}, '') = '' THEN 1 ELSE 0 END) AS 未核查,
                SUM(CASE WHEN IFNULL(t.现住址, '') <> '' AND IFNULL(t.{rc}, '') = '' THEN 1 ELSE 0 END) AS 已核查,
                SUM(CASE WHEN IFNULL(t.{rc}, '') <> '' THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN IFNULL(t.{rc}, '') <> '' THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查完成率,
                SUM(CASE WHEN {no_base} THEN 1 ELSE 0 END) AS 无法见底数,
                CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(CASE WHEN {see_base} THEN 1 ELSE 0 END) / COUNT(*), 2) ELSE 0 END AS 核查见底率
            FROM {src} t
            WHERE t.社区 IS NOT NULL AND t.社区 <> '' AND t.社区 <> '社区'
            GROUP BY t.社区
            ORDER BY t.社区
        """

        return inspector_sql, community_sql
