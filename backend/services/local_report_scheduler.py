"""Refresh online-data snapshots and reports from the local business tables."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any, Iterable

from database import db_manager
from services.business_time import get_business_date
from services.report_builders import BUILDERS
from services.report_builders.summary import _load_summary_types, build_summary
from services.report_table_utils import table_exists
from config import settings


REPORT_REFRESH_LOCK = "binhu_local_daily_report_refresh"
GENERATION_METHOD = "local_scheduler"
REFRESH_INTERVAL_SECONDS = 600
_SOURCE_TABLE = re.compile(r"^t_[a-z0-9_]+$")
_SUFFIX = re.compile(r"^[A-Za-z0-9]+$")


def _identifier(value: str) -> str:
    return f"`{value}`"


def _schema(value: str) -> str:
    """Quote a configured schema name after rejecting SQL metacharacters."""
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", text):
        raise ValueError(f"非法数据库名：{value}")
    return _identifier(text)


def _validated_builders(
    parser_types: Iterable[str] | None = None,
) -> list[tuple[str, Any]]:
    selected = list(parser_types or BUILDERS.keys())
    result: list[tuple[str, Any]] = []
    for parser_type in selected:
        builder = BUILDERS.get(parser_type)
        if builder is None:
            raise ValueError(f"不支持的日报类型：{parser_type}")
        if not _SOURCE_TABLE.fullmatch(str(builder.source_table)):
            raise ValueError(f"非法业务表名：{builder.source_table}")
        if not _SUFFIX.fullmatch(str(builder.table_suffix)):
            raise ValueError(f"非法日报后缀：{builder.table_suffix}")
        result.append((parser_type, builder))
    return result


async def _acquire_refresh_lock(conn, timeout_seconds: int = 0) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT GET_LOCK(%s, %s)",
            (REPORT_REFRESH_LOCK, timeout_seconds),
        )
        row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def _release_refresh_lock(conn) -> None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT RELEASE_LOCK(%s)", (REPORT_REFRESH_LOCK,))
        await cur.fetchone()


async def replace_local_report_snapshots(
    conn,
    parser_types: Iterable[str] | None = None,
) -> str:
    """Atomically replace the current business day's local-table snapshots."""
    builders = _validated_builders(parser_types)
    online_schema = _schema(settings.MYSQL_ONLINE_DATA_DB)
    report_schema = _schema(settings.MYSQL_DAILY_REPORT_DB)
    staged = [
        (parser_type, f"{{report_date}}_snapshot_{builder.table_suffix}", builder.source_table)
        for parser_type, builder in builders
    ]

    async with conn.cursor() as cur:
        report_date = (await get_business_date(cur)).isoformat()
        staged = [
            (parser_type, final_pattern.format(report_date=report_date), source)
            for parser_type, final_pattern, source in staged
        ]
        # Stable snapshot tables avoid the metadata-lock convoy caused by a
        # runtime RENAME TABLE.  DELETE/INSERT are transactional InnoDB
        # operations and readers continue to see the previous committed
        # snapshot while a refresh is in progress.
        await conn.begin()
        try:
            for _, final_name, source_table in staged:
                if not await table_exists(cur, settings.MYSQL_DAILY_REPORT_DB, final_name):
                    await cur.execute(
                        f"CREATE TABLE {report_schema}.{_identifier(final_name)} LIKE "
                        f"{online_schema}.{_identifier(source_table)}"
                    )
                await cur.execute(
                    f"DELETE FROM {report_schema}.{_identifier(final_name)}"
                )
                await cur.execute(
                    f"INSERT INTO {report_schema}.{_identifier(final_name)} "
                    f"SELECT * FROM {online_schema}.{_identifier(source_table)}"
                )

            metadata_values = ", ".join(["(%s, %s, %s, 'snapshot')"] * len(staged))
            metadata_params: list[str] = []
            for parser_type, final_name, _ in staged:
                metadata_params.extend([final_name, report_date, f"{parser_type}_snapshot"])
            await cur.execute(
                f"INSERT INTO {report_schema}._daily_report_meta "
                "(table_name, report_date, parser_type, generation_method) "
                f"VALUES {metadata_values} "
                "ON DUPLICATE KEY UPDATE generated_at=NOW(), generation_method='snapshot'",
                metadata_params,
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    return report_date


async def refresh_local_daily_reports_once() -> dict[str, Any]:
    """Refresh all local snapshots, subreports and the configured summary."""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    locked = False
    try:
        locked = await _acquire_refresh_lock(conn)
        if not locked:
            return {"status": "busy", "report_date": None, "report_types": []}

        report_types = list(BUILDERS.keys())
        report_date = await replace_local_report_snapshots(conn, report_types)
        subreports: list[dict[str, Any]] = []
        for parser_type in report_types:
            result = await BUILDERS[parser_type].build(
                report_date,
                generation_method=GENERATION_METHOD,
            )
            if result.get("implemented") is False:
                raise RuntimeError(
                    result.get("message")
                    or f"{parser_type} 分汇总表生成失败"
                )
            subreports.append({"parser_type": parser_type, **result})

        summary_types = await _load_summary_types()
        summary = await build_summary(
            report_date,
            summary_types=summary_types,
            generation_method=GENERATION_METHOD,
        )
        if summary.get("implemented") is False:
            raise RuntimeError(summary.get("message") or "总汇总表生成失败")
        return {
            "status": "success",
            "report_date": report_date,
            "report_types": report_types,
            "subreports": subreports,
            "summary": summary,
        }
    finally:
        try:
            if locked:
                await _release_refresh_lock(conn)
        finally:
            pool.release(conn)


async def run_local_report_scheduler() -> None:
    """Run immediately on startup, then refresh at the configured interval."""
    while True:
        try:
            result = await refresh_local_daily_reports_once()
            if result["status"] == "success":
                print(
                    "[LOCAL_REPORT] 日报已刷新："
                    f"{result['report_date']}，{len(result['report_types'])} 个业务类型"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "[LOCAL_REPORT] 日报刷新失败："
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
