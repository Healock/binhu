"""疑似未注销模型三日报生成器。

模型三没有“现住址”这一中间核查字段，核查结果只有：
近期反吴、在吴、离吴。因此结果为空时算未核查，出现任一有效结果时算已完成，
“已核查”固定为 0。
"""

from .base import BaseReportBuilder


class SuspectUnrevokedBuilder(BaseReportBuilder):
    parser_type = "疑似未注销模型三"
    source_table = "t_suspect_unrevoked"
    table_suffix = "suspectUnrevoked"
    community_column = "下发社区"
    result_column = "核查结果"
    valid_results = ("近期反吴", "在吴", "离吴")
    see_base_keywords = list(valid_results)

    def ledger_state_sql(self, alias: str) -> str:
        return (
            f"CASE WHEN {self._valid_result_sql(alias)} THEN 'completed' "
            "ELSE 'unchecked' END"
        )

    def ledger_change_sql(self, today_alias: str, previous_alias: str) -> str:
        return (
            f"TRIM(IFNULL({previous_alias}.`{self.result_column}`, '')) "
            f"<> TRIM(IFNULL({today_alias}.`{self.result_column}`, ''))"
        )

    def ledger_unable_sql(self, alias: str) -> str:
        return "0"

    def ledger_reached_bottom_sql(self, alias: str) -> str:
        return f"CASE WHEN {self._valid_result_sql(alias)} THEN 1 ELSE 0 END"

    def _valid_result_sql(self, alias: str) -> str:
        values = ", ".join(f"'{value}'" for value in self.valid_results)
        return (
            f"TRIM(IFNULL({alias}.`{self.result_column}`, '')) "
            f"IN ({values})"
        )

    def _build_workload_sql(
        self, today: str, prev: str | None
    ) -> tuple[str, str]:
        """按核查结果统计当天新增或结果发生变化的数据。"""
        valid_result = self._valid_result_sql("t")
        unresolved = f"NOT ({valid_result})"

        if prev:
            join_clause = f"LEFT JOIN {prev} prev ON t._row_key = prev._row_key"
            change_filter = f"""(
                prev._row_key IS NULL
                OR TRIM(IFNULL(prev.`{self.result_column}`, ''))
                   <> TRIM(IFNULL(t.`{self.result_column}`, ''))
            )"""
        else:
            join_clause = ""
            change_filter = (
                "t._last_updated_at >= %s AND t._last_updated_at < %s"
            )

        inspector_sql = f"""
            SELECT
                t.`{self.community_column}`, t.核查人, COUNT(*),
                SUM(CASE WHEN {unresolved} THEN 1 ELSE 0 END),
                0,
                SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0
                     THEN ROUND(SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) / COUNT(*), 2)
                     ELSE 0 END,
                0,
                CASE WHEN SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) > 0
                     THEN 1.00
                     ELSE 0 END
            FROM {today} t
            {join_clause}
            WHERE t.核查人 IS NOT NULL AND t.核查人 <> '' AND t.核查人 <> '核查人'
              AND t.`{self.community_column}` IS NOT NULL
              AND t.`{self.community_column}` <> ''
              AND t.`{self.community_column}` <> '{self.community_column}'
              AND {change_filter}
            GROUP BY t.`{self.community_column}`, t.核查人
            ORDER BY t.`{self.community_column}`, t.核查人
        """

        community_sql = f"""
            SELECT
                t.`{self.community_column}`, COUNT(*),
                SUM(CASE WHEN {unresolved} THEN 1 ELSE 0 END),
                0,
                SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END),
                CASE WHEN COUNT(*) > 0
                     THEN ROUND(SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) / COUNT(*), 2)
                     ELSE 0 END,
                0,
                CASE WHEN SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) > 0
                     THEN 1.00
                     ELSE 0 END
            FROM {today} t
            {join_clause}
            WHERE t.`{self.community_column}` IS NOT NULL
              AND t.`{self.community_column}` <> ''
              AND t.`{self.community_column}` <> '{self.community_column}'
              AND {change_filter}
            GROUP BY t.`{self.community_column}`
            ORDER BY t.`{self.community_column}`
        """

        return inspector_sql, community_sql

    def build_stats_sql(self, src: str) -> tuple[str, str]:
        """生成区间查询使用的最终状态统计 SQL。"""
        valid_result = self._valid_result_sql("t")
        unresolved = f"NOT ({valid_result})"

        inspector_sql = f"""
            SELECT
                t.`{self.community_column}` AS 社区,
                t.核查人 AS 姓名,
                COUNT(*) AS 数据总数,
                SUM(CASE WHEN {unresolved} THEN 1 ELSE 0 END) AS 未核查,
                0 AS 已核查,
                SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0
                     THEN ROUND(SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) / COUNT(*), 2)
                     ELSE 0 END AS 核查完成率,
                0 AS 无法见底数,
                CASE WHEN SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) > 0
                     THEN 1.00
                     ELSE 0 END AS 核查见底率
            FROM {src} t
            WHERE t.核查人 IS NOT NULL AND t.核查人 <> '' AND t.核查人 <> '核查人'
              AND t.`{self.community_column}` IS NOT NULL
              AND t.`{self.community_column}` <> ''
              AND t.`{self.community_column}` <> '{self.community_column}'
            GROUP BY t.`{self.community_column}`, t.核查人
            ORDER BY t.`{self.community_column}`, t.核查人
        """

        community_sql = f"""
            SELECT
                t.`{self.community_column}` AS 社区,
                COUNT(*) AS 数据总数,
                SUM(CASE WHEN {unresolved} THEN 1 ELSE 0 END) AS 未核查,
                0 AS 已核查,
                SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) AS 已完成,
                CASE WHEN COUNT(*) > 0
                     THEN ROUND(SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) / COUNT(*), 2)
                     ELSE 0 END AS 核查完成率,
                0 AS 无法见底数,
                CASE WHEN SUM(CASE WHEN {valid_result} THEN 1 ELSE 0 END) > 0
                     THEN 1.00
                     ELSE 0 END AS 核查见底率
            FROM {src} t
            WHERE t.`{self.community_column}` IS NOT NULL
              AND t.`{self.community_column}` <> ''
              AND t.`{self.community_column}` <> '{self.community_column}'
            GROUP BY t.`{self.community_column}`
            ORDER BY t.`{self.community_column}`
        """

        return inspector_sql, community_sql
