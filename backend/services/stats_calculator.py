"""日报管理器 - 调度 report_builders 生成和查看日报"""

from database import db_manager
from services.report_builders import get_builder, IMPLEMENTED_TYPES, BUILDERS
from services.report_members import (
    complete_inspector_rows,
)


class DailyReportBuilder:
    """日报调度器：按类型分派到对应的 report_builder"""

    # 类型 → 表名后缀映射（用于 get_report 查表）
    TYPE_SUFFIX = {pt: b.table_suffix for pt, b in BUILDERS.items()}

    async def build(self, date_str: str, parser_type: str = "全链条") -> dict:
        """生成指定类型的日报"""
        builder = get_builder(parser_type)
        if not builder:
            return {"implemented": False, "message": f"{parser_type}的统计规则尚未实现"}
        result = await builder.build(date_str)
        result.setdefault("implemented", True)
        return result

    async def get_report(self, date_str: str, parser_type: str = "全链条") -> dict:
        """查看日报（返回两张表的数据）"""
        suffix = self.TYPE_SUFFIX.get(parser_type)
        if not suffix:
            return {"exists": False, "message": f"{parser_type}的统计规则尚未实现"}
        t_inspector = f"`{date_str}_daily_{suffix}_inspector`"
        t_community = f"`{date_str}_daily_{suffix}_community`"
        pool = db_manager.get_pool("daily_report")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta WHERE table_name = %s",
                    (f"{date_str}_snapshot_{suffix}",),
                )
                if not await cur.fetchone():
                    return {
                        "exists": False,
                        "message": f"{date_str} 没有同步快照，暂无日报",
                    }

                await cur.execute(
                    "SELECT table_name FROM _daily_report_meta WHERE table_name = %s",
                    (f"{date_str}_daily_{suffix}_inspector",),
                )
                if not await cur.fetchone():
                    return {
                        "exists": False,
                        "message": f"{date_str} 尚未生成「{parser_type}」日报",
                    }

                await cur.execute(f"SELECT * FROM {t_inspector} ORDER BY 社区, 姓名")
                insp_rows = await cur.fetchall()
                await cur.execute(f"SHOW COLUMNS FROM {t_inspector}")
                insp_cols = [c[0] for c in await cur.fetchall()]
                insp_rows = await complete_inspector_rows(cur, insp_rows, date_str)

                await cur.execute(f"SELECT * FROM {t_community} ORDER BY 社区")
                comm_rows = await cur.fetchall()
                await cur.execute(f"SHOW COLUMNS FROM {t_community}")
                comm_cols = [c[0] for c in await cur.fetchall()]

            return {
                "exists": True,
                "inspector": {"columns": insp_cols, "data": [dict(zip(insp_cols, r)) for r in insp_rows]},
                "community": {"columns": comm_cols, "data": [dict(zip(comm_cols, r)) for r in comm_rows]},
            }
        finally:
            pool.release(conn)

    async def list_reports(self) -> list:
        pool = db_manager.get_pool("daily_report")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT report_date, parser_type, generation_method, "
                    "DATE_FORMAT(MAX(generated_at), '%Y-%m-%d %H:%i') "
                    "FROM _daily_report_meta GROUP BY report_date, parser_type, generation_method ORDER BY report_date DESC"
                )
                rows = await cur.fetchall()
            return [{"date": str(r[0]), "type": r[1], "method": r[2], "generated_at": r[3]} for r in rows]
        finally:
            pool.release(conn)
