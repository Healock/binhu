"""Read-only metadata for the three application databases."""

from datetime import datetime
import re

from database import db_manager


DATABASES = {
    "OnlineData": "当前业务数据和系统配置",
    "OnlineDataArchive": "从在线表格移除的历史数据",
    "daily_report": "每日快照和统计报表",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")


def _iso_utc(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def _last_activity(cur, database_name: str) -> datetime | None:
    if database_name == "OnlineData":
        await cur.execute(
            """
            SELECT MAX(activity_at) FROM (
                SELECT MAX(finished_at) AS activity_at FROM OnlineData._sync_log
                UNION ALL
                SELECT MAX(updated_at) FROM OnlineData._config_spreadsheets
                UNION ALL
                SELECT MAX(updated_at) FROM OnlineData._config_oauth_tokens
                UNION ALL
                SELECT MAX(updated_at) FROM OnlineData._users
                UNION ALL
                SELECT MAX(updated_at) FROM OnlineData._grid_members
                UNION ALL
                SELECT MAX(finished_at) FROM OnlineData._visit_import_batches
            ) activity
            """
        )
        row = await cur.fetchone()
        return row[0] if row else None

    if database_name == "daily_report":
        await cur.execute(
            "SELECT MAX(generated_at) FROM daily_report._daily_report_meta"
        )
        row = await cur.fetchone()
        return row[0] if row else None

    await cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='OnlineDataArchive'
          AND table_name LIKE %s
        """,
        ("%\\_archive",),
    )
    latest = None
    for (table_name,) in await cur.fetchall():
        if not SAFE_IDENTIFIER.fullmatch(table_name):
            continue
        await cur.execute(
            f"SELECT MAX(_archived_at) "
            f"FROM `OnlineDataArchive`.`{table_name}`"
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
            await cur.execute(
                """
                SELECT table_schema, COUNT(*),
                       COALESCE(SUM(table_rows), 0),
                       COALESCE(SUM(data_length), 0),
                       COALESCE(SUM(index_length), 0),
                       MAX(update_time)
                FROM information_schema.tables
                WHERE table_schema IN (
                    'OnlineData', 'OnlineDataArchive', 'daily_report'
                )
                GROUP BY table_schema
                """
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
            await cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_connected'")
            connected_row = await cur.fetchone()
            return {
                "connected": True,
                "version": version,
                "connections": int(connected_row[1]) if connected_row else 0,
                "max_connections": int(max_connections),
            }
    finally:
        pool.release(conn)
