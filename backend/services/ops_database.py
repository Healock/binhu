"""Read-only metadata for the eight application databases."""

from datetime import datetime
import re

from database import db_manager
from config import settings


DATABASES = {
    settings.MYSQL_PLATFORM_DB: "账号、人员、权限、通知、审计、排班和平台配置",
    settings.MYSQL_ONLINE_DATA_DB: "腾讯配置、同步、来源投影、在线业务表和回写记录",
    settings.MYSQL_ARCHIVE_DB: "从在线表格移除的只读历史数据",
    settings.MYSQL_DAILY_REPORT_DB: "每日快照、任务流水和工作日志草稿",
    settings.MYSQL_VISIT_DB: "走访批次、明细和导入异常",
    settings.MYSQL_DISPATCH_DB: "下发批次、任务和发布结果",
    settings.MYSQL_REGISTRY_DB: "辖区档案、小区地址、人员标签和任务标签快照",
    settings.MYSQL_WORKFLOW_DB: "流程配置、工单、事件和附件元数据",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")


def _iso_utc(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def _last_activity(cur, database_name: str) -> datetime | None:
    if database_name == settings.MYSQL_ONLINE_DATA_DB:
        await cur.execute(
            f"""
            SELECT MAX(activity_at) FROM (
                SELECT MAX(finished_at) AS activity_at FROM `{settings.MYSQL_ONLINE_DATA_DB}`._sync_log
                UNION ALL
                SELECT MAX(updated_at) FROM `{settings.MYSQL_ONLINE_DATA_DB}`._config_spreadsheets
                UNION ALL
                SELECT MAX(updated_at) FROM `{settings.MYSQL_ONLINE_DATA_DB}`._config_oauth_tokens
            ) activity
            """
        )
        row = await cur.fetchone()
        return row[0] if row else None

    if database_name == settings.MYSQL_DAILY_REPORT_DB:
        await cur.execute(
            f"SELECT MAX(generated_at) FROM `{settings.MYSQL_DAILY_REPORT_DB}`._daily_report_meta"
        )
        row = await cur.fetchone()
        return row[0] if row else None

    if database_name != settings.MYSQL_ARCHIVE_DB:
        await cur.execute(
            "SELECT MAX(update_time) FROM information_schema.tables WHERE table_schema=%s",
            (database_name,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    await cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema=%s
          AND table_name LIKE %s
        """,
        (settings.MYSQL_ARCHIVE_DB, "%\\_archive"),
    )
    latest = None
    for (table_name,) in await cur.fetchall():
        if not SAFE_IDENTIFIER.fullmatch(table_name):
            continue
        await cur.execute(
            f"SELECT MAX(_archived_at) "
            f"FROM `{settings.MYSQL_ARCHIVE_DB}`.`{table_name}`"
        )
        row = await cur.fetchone()
        if row and row[0] and (latest is None or row[0] > latest):
            latest = row[0]
    return latest


async def get_database_overview() -> list[dict]:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(DATABASES))
            await cur.execute(
                f"""
                SELECT table_schema, COUNT(*),
                       COALESCE(SUM(table_rows), 0),
                       COALESCE(SUM(data_length), 0),
                       COALESCE(SUM(index_length), 0),
                       MAX(update_time)
                FROM information_schema.tables
                WHERE table_schema IN ({placeholders})
                GROUP BY table_schema
                """,
                tuple(DATABASES),
            )
            by_name = {row[0]: row for row in await cur.fetchall()}
            result = []
            for name, purpose in DATABASES.items():
                row = by_name.get(name)
                result.append(
                    {
                        "name": name,
                        "purpose": purpose,
                        "table_count": row[1] if row else 0,
                        "estimated_rows": int(row[2] or 0) if row else 0,
                        "data_bytes": int(row[3] or 0) if row else 0,
                        "index_bytes": int(row[4] or 0) if row else 0,
                        "engine_update_at": _iso_utc(row[5]) if row else None,
                        "last_activity_at": _iso_utc(
                            await _last_activity(cur, name)
                        ),
                    }
                )
            return result
    finally:
        pool.release(conn)


def require_database_name(database_name: str) -> str:
    if database_name not in DATABASES:
        raise ValueError("未知数据库")
    return database_name


async def list_database_tables(database_name: str) -> list[dict]:
    database_name = require_database_name(database_name)
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name, engine, table_rows, data_length,
                       index_length, update_time, table_collation
                FROM information_schema.tables
                WHERE table_schema=%s
                ORDER BY table_name
                """,
                (database_name,),
            )
            return [
                {
                    "name": row[0],
                    "engine": row[1],
                    "estimated_rows": int(row[2] or 0),
                    "data_bytes": int(row[3] or 0),
                    "index_bytes": int(row[4] or 0),
                    "engine_update_at": _iso_utc(row[5]),
                    "collation": row[6],
                }
                for row in await cur.fetchall()
            ]
    finally:
        pool.release(conn)


async def get_table_structure(
    database_name: str,
    table_name: str,
) -> dict | None:
    database_name = require_database_name(database_name)
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
                """,
                (database_name, table_name),
            )
            if not (await cur.fetchone())[0]:
                return None
            await cur.execute(
                """
                SELECT column_name, column_type, is_nullable,
                       column_default, column_key, extra
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position
                """,
                (database_name, table_name),
            )
            columns = [
                {
                    "name": row[0],
                    "type": row[1],
                    "nullable": row[2] == "YES",
                    "default": row[3],
                    "key": row[4],
                    "extra": row[5],
                }
                for row in await cur.fetchall()
            ]
            await cur.execute(
                """
                SELECT index_name, non_unique, seq_in_index, column_name,
                       index_type
                FROM information_schema.statistics
                WHERE table_schema=%s AND table_name=%s
                ORDER BY index_name, seq_in_index
                """,
                (database_name, table_name),
            )
            indexes = [
                {
                    "name": row[0],
                    "unique": not bool(row[1]),
                    "position": row[2],
                    "column": row[3],
                    "type": row[4],
                }
                for row in await cur.fetchall()
            ]
            return {
                "database": database_name,
                "table": table_name,
                "columns": columns,
                "indexes": indexes,
            }
    finally:
        pool.release(conn)


async def get_mysql_status() -> dict:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT VERSION(), @@max_connections")
            version, max_connections = await cur.fetchone()
            await cur.execute(
                "SHOW GLOBAL STATUS WHERE Variable_name IN "
                "('Threads_connected','Threads_running',"
                "'Innodb_row_lock_current_waits','Slow_queries')"
            )
            status_rows = {
                str(row[0]): int(row[1] or 0)
                for row in await cur.fetchall()
            }
            return {
                "connected": True,
                "version": version,
                "connections": status_rows.get("Threads_connected", 0),
                "max_connections": int(max_connections),
                "threads_running": status_rows.get("Threads_running", 0),
                "lock_waits": status_rows.get(
                    "Innodb_row_lock_current_waits", 0
                ),
                "slow_queries": status_rows.get("Slow_queries", 0),
            }
    finally:
        pool.release(conn)
