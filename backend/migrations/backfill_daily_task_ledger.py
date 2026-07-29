"""回算在线日报任务流水，并安全替换 0.1.0 历史日报。

默认只审计，不写数据库。只有显式传入 ``--apply`` 才会：
1. 按时间顺序回算所有已支持业务的任务流水；
2. 在 ``_v020_`` 临时表生成新版人员、社区和总汇总；
3. 验证通过后，用一次 RENAME TABLE 原子替换；
4. 把旧日报保留为 ``_v010_`` 表，供快速回退。

运行本脚本前必须先停止后端，并完成 daily_report 备份。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiomysql

from config import settings
from services.grid_member_status import active_member_sql
from services.personnel_positions import (
    ONLINE_POSITION_CONFIG_KEY,
    get_configured_positions,
)
from services.report_builders import BUILDERS
from services.report_builders.base import BaseReportBuilder
from services.report_builders.summary import SUMMARY_COLS
from services.report_ledger import (
    aggregate_ledger_into_reports,
    refresh_daily_ledger,
)


PUBLIC_TABLE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_daily_"
    r"(?:[A-Za-z0-9]+_(?:inspector|community)|summary)$"
)
SNAPSHOT_TABLE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_snapshot_[A-Za-z0-9]+$"
)
TEMP_PREFIX = "_v020_"
BACKUP_PREFIX = "_v010_"


@dataclass(frozen=True)
class Snapshot:
    report_date: str
    parser_type: str
    table_name: str
    generated_at: Any


def _quoted(table_name: str) -> str:
    if not (
        PUBLIC_TABLE.fullmatch(table_name)
        or SNAPSHOT_TABLE.fullmatch(table_name)
        or table_name.startswith(TEMP_PREFIX)
        or table_name.startswith(BACKUP_PREFIX)
    ):
        raise ValueError(f"非法日报表名: {table_name}")
    if len(table_name) > 64:
        raise ValueError(f"日报表名超过 MySQL 限制: {table_name}")
    return f"`{table_name}`"


def _stage_name(public_name: str) -> str:
    if not PUBLIC_TABLE.fullmatch(public_name):
        raise ValueError(f"非法公开日报表名: {public_name}")
    return f"{TEMP_PREFIX}{public_name}"


def _backup_name(public_name: str) -> str:
    if not PUBLIC_TABLE.fullmatch(public_name):
        raise ValueError(f"非法公开日报表名: {public_name}")
    return f"{BACKUP_PREFIX}{public_name}"


async def _table_exists(cur, table_name: str) -> bool:
    await cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table_name,),
    )
    return bool((await cur.fetchone())[0])


async def _table_columns(cur, table_name: str) -> set[str]:
    await cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (table_name,),
    )
    return {str(row[0]) for row in await cur.fetchall()}


async def _load_snapshots(cur) -> list[Snapshot]:
    await cur.execute(
        "SELECT table_name, report_date, parser_type, generated_at "
        "FROM _daily_report_meta "
        "WHERE RIGHT(parser_type, 9) = '_snapshot' "
        "ORDER BY report_date, parser_type, table_name"
    )
    snapshots = []
    for table_name, report_date, meta_type, generated_at in await cur.fetchall():
        parser_type = str(meta_type)[:-9]
        if parser_type not in BUILDERS:
            continue
        snapshots.append(
            Snapshot(
                report_date=str(report_date),
                parser_type=parser_type,
                table_name=str(table_name),
                generated_at=generated_at,
            )
        )
    return snapshots


async def _audit_snapshots(cur) -> tuple[list[Snapshot], list[str]]:
    snapshots = await _load_snapshots(cur)
    errors: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    dates_by_type: dict[str, list[date]] = defaultdict(list)

    if not snapshots:
        errors.append("没有找到任何已支持业务的历史快照")

    for snapshot in snapshots:
        pair = (snapshot.report_date, snapshot.parser_type)
        if pair in seen_pairs:
            errors.append(
                f"{snapshot.report_date} {snapshot.parser_type} "
                "存在多张快照"
            )
            continue
        seen_pairs.add(pair)
        dates_by_type[snapshot.parser_type].append(
            date.fromisoformat(snapshot.report_date)
        )

        builder = BUILDERS[snapshot.parser_type]
        expected_name = (
            f"{snapshot.report_date}_snapshot_{builder.table_suffix}"
        )
        if snapshot.table_name != expected_name:
            errors.append(
                f"{snapshot.parser_type} 快照名与注册规则不符："
                f"{snapshot.table_name}"
            )
            continue
        if snapshot.generated_at is None:
            errors.append(f"{snapshot.table_name} 缺少生成时间")
        if not await _table_exists(cur, snapshot.table_name):
            errors.append(f"快照表不存在：{snapshot.table_name}")
            continue

        required = {
            "_row_key",
            "_first_seen_at",
            "_last_updated_at",
            builder.community_column,
            builder.inspector_column,
            builder.result_column,
        }
        if builder.community_column != "下发社区":
            required.add("现住址")
        columns = await _table_columns(cur, snapshot.table_name)
        missing_columns = sorted(required - columns)
        if missing_columns:
            errors.append(
                f"{snapshot.table_name} 缺少字段："
                + "、".join(missing_columns)
            )
            continue

        await cur.execute(
            f"SELECT COUNT(*) FROM ("
            f"SELECT _row_key FROM {_quoted(snapshot.table_name)} "
            "GROUP BY _row_key HAVING COUNT(*) > 1"
            ") duplicates"
        )
        if (await cur.fetchone())[0]:
            errors.append(f"{snapshot.table_name} 存在重复 _row_key")

        archive_name = f"{builder.source_table}_archive"
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s "
            "AND column_name IN ('_row_key', '_archived_at')",
            (settings.MYSQL_ARCHIVE_DB, archive_name),
        )
        archive_columns = {str(row[0]) for row in await cur.fetchall()}
        if archive_columns != {"_row_key", "_archived_at"}:
            errors.append(
                f"{settings.MYSQL_ARCHIVE_DB}.{archive_name} "
                "缺少归档判断字段"
            )
            continue

        await cur.execute(
            f"SELECT COUNT(*) FROM "
            f"{settings.MYSQL_ARCHIVE_DB}.`{archive_name}` "
            "WHERE _archived_at IS NULL"
        )
        if (await cur.fetchone())[0]:
            errors.append(f"{archive_name} 存在无法判断时间的归档记录")

    gaps: dict[str, list[str]] = {}
    for parser_type, report_dates in dates_by_type.items():
        sorted_dates = sorted(set(report_dates))
        missing = []
        for previous, current in zip(sorted_dates, sorted_dates[1:]):
            gap_days = (current - previous).days
            if gap_days > 1:
                missing.append(
                    f"{previous.isoformat()} 至 {current.isoformat()}"
                )
        if missing:
            gaps[parser_type] = missing

    if gaps:
        errors.append(
            "快照断档（允许使用最近一张更早快照）："
            + json.dumps(gaps, ensure_ascii=False)
        )
    return snapshots, errors


async def _ensure_ledger_tables(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _daily_task_ledger (
            report_date DATE NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(200) NOT NULL,
            source VARCHAR(20) NOT NULL,
            included TINYINT(1) NOT NULL DEFAULT 1,
            online_present TINYINT(1) NOT NULL DEFAULT 1,
            community VARCHAR(200) DEFAULT '',
            inspector VARCHAR(100) DEFAULT '',
            task_state VARCHAR(20) NOT NULL,
            unable_to_verify TINYINT(1) NOT NULL DEFAULT 0,
            reached_bottom TINYINT(1) NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (report_date, parser_type, row_key),
            INDEX idx_ledger_type_date (parser_type, report_date),
            INDEX idx_ledger_person (report_date, community, inspector)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _daily_task_ledger_runs (
            report_date DATE NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            snapshot_table VARCHAR(100) NOT NULL,
            previous_snapshot_table VARCHAR(100) DEFAULT NULL,
            ledger_rows INT NOT NULL DEFAULT 0,
            included_rows INT NOT NULL DEFAULT 0,
            generation_method VARCHAR(20) NOT NULL DEFAULT 'sync',
            generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (report_date, parser_type),
            INDEX idx_ledger_run_date (report_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )


async def _create_stage_reports(
    cur,
    snapshots: list[Snapshot],
) -> dict[tuple[str, str], tuple[str, str]]:
    stage_tables: dict[tuple[str, str], tuple[str, str]] = {}
    for snapshot in snapshots:
        builder = BUILDERS[snapshot.parser_type]
        inspector_public = (
            f"{snapshot.report_date}_daily_"
            f"{builder.table_suffix}_inspector"
        )
        community_public = (
            f"{snapshot.report_date}_daily_"
            f"{builder.table_suffix}_community"
        )
        inspector_stage = _stage_name(inspector_public)
        community_stage = _stage_name(community_public)

        await cur.execute(f"DROP TABLE IF EXISTS {_quoted(inspector_stage)}")
        await cur.execute(f"DROP TABLE IF EXISTS {_quoted(community_stage)}")
        await cur.execute(
            f"CREATE TABLE {_quoted(inspector_stage)} "
            f"({BaseReportBuilder.INSPECTOR_COLS}) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COLLATE=utf8mb4_unicode_ci"
        )
        await cur.execute(
            f"CREATE TABLE {_quoted(community_stage)} "
            f"({BaseReportBuilder.COMMUNITY_COLS}) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COLLATE=utf8mb4_unicode_ci"
        )

        await refresh_daily_ledger(
            cur,
            builder,
            snapshot.report_date,
            generation_method="backfill",
            reset=True,
        )
        await aggregate_ledger_into_reports(
            cur,
            builder,
            snapshot.report_date,
            _quoted(inspector_stage),
            _quoted(community_stage),
        )
        await _validate_stage_pair(
            cur,
            snapshot,
            inspector_stage,
            community_stage,
        )
        stage_tables[
            (snapshot.report_date, snapshot.parser_type)
        ] = (inspector_stage, community_stage)
    return stage_tables


async def _validate_stage_pair(
    cur,
    snapshot: Snapshot,
    inspector_stage: str,
    community_stage: str,
) -> None:
    await cur.execute(
        f"SELECT COUNT(*) FROM {_quoted(inspector_stage)} "
        "WHERE 数据总数 <> 未核查 + 已核查 + 已完成"
    )
    if (await cur.fetchone())[0]:
        raise RuntimeError(
            f"{inspector_stage} 的三列之和不等于数据总数"
        )

    await cur.execute(
        f"SELECT COUNT(*) FROM {_quoted(community_stage)} "
        "WHERE 数据总数 <> 未核查 + 已核查 + 已完成"
    )
    if (await cur.fetchone())[0]:
        raise RuntimeError(
            f"{community_stage} 的三列之和不等于数据总数"
        )

    await cur.execute(
        """
        SELECT
            COALESCE(SUM(
                included=1
                AND inspector <> ''
                AND inspector <> '核查人'
                AND community <> ''
                AND community <> '社区'
                AND community <> '下发社区'
            ), 0)
        FROM _daily_task_ledger
        WHERE report_date=%s AND parser_type=%s
        """,
        (snapshot.report_date, snapshot.parser_type),
    )
    expected = int((await cur.fetchone())[0] or 0)
    await cur.execute(
        f"SELECT COALESCE(SUM(数据总数), 0) "
        f"FROM {_quoted(inspector_stage)}"
    )
    actual = int((await cur.fetchone())[0] or 0)
    if expected != actual:
        raise RuntimeError(
            f"{inspector_stage} 聚合数量不一致："
            f"流水 {expected}，日报 {actual}"
        )


async def _load_summary_types(cur) -> list[str]:
    await cur.execute(
        "SELECT config_value FROM OnlineData._system_config "
        "WHERE config_key='summary_types'"
    )
    row = await cur.fetchone()
    if not row or not row[0]:
        return list(BUILDERS)
    try:
        configured = json.loads(row[0])
    except Exception:
        return list(BUILDERS)
    return [
        parser_type
        for parser_type in configured
        if parser_type in BUILDERS
    ] or list(BUILDERS)


async def _create_stage_summaries(
    cur,
    snapshots: list[Snapshot],
    stage_tables: dict[tuple[str, str], tuple[str, str]],
) -> dict[str, str]:
    summary_types = await _load_summary_types(cur)
    dates = sorted({snapshot.report_date for snapshot in snapshots})
    summary_stages: dict[str, str] = {}

    for report_date in dates:
        community_stages = [
            stage_tables[(report_date, parser_type)][1]
            for parser_type in summary_types
            if (report_date, parser_type) in stage_tables
        ]
        if not community_stages:
            continue

        public_name = f"{report_date}_daily_summary"
        stage_name = _stage_name(public_name)
        await cur.execute(f"DROP TABLE IF EXISTS {_quoted(stage_name)}")
        await cur.execute(
            f"CREATE TABLE {_quoted(stage_name)} ({SUMMARY_COLS}) "
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COLLATE=utf8mb4_unicode_ci"
        )

        union_sql = " UNION ALL ".join(
            "SELECT 社区, 数据总数, 未核查, 已核查, 已完成, "
            f"无法见底数 FROM {_quoted(table_name)}"
            for table_name in community_stages
        )
        positions = await get_configured_positions(
            cur,
            ONLINE_POSITION_CONFIG_KEY,
        )
        placeholders = ", ".join(["%s"] * len(positions))
        active_condition = active_member_sql()
        await cur.execute(
            f"""
            INSERT INTO {_quoted(stage_name)} (
                社区, 数据总数, 未核查, 已核查, 已完成,
                无法见底数, 网格员人数
            )
            SELECT
                source_rows.社区,
                SUM(source_rows.数据总数),
                SUM(source_rows.未核查),
                SUM(source_rows.已核查),
                SUM(source_rows.已完成),
                SUM(source_rows.无法见底数),
                COALESCE((
                    SELECT COUNT(*)
                    FROM OnlineData._grid_members
                    WHERE community=source_rows.社区
                      AND position IN ({placeholders})
                      AND {active_condition}
                ), 0)
            FROM ({union_sql}) source_rows
            GROUP BY source_rows.社区
            """,
            (*positions, report_date),
        )
        await cur.execute(
            f"""
            UPDATE {_quoted(stage_name)}
            SET
                核查完成率=CASE
                    WHEN 数据总数 > 0
                    THEN ROUND(已完成 / 数据总数, 2)
                    ELSE 0
                END,
                核查见底率=CASE
                    WHEN 已完成 > 0
                    THEN ROUND(
                        GREATEST(已完成 - 无法见底数, 0)
                        / 已完成,
                        2
                    )
                    ELSE 0
                END,
                当日人均核查数=CASE
                    WHEN 网格员人数 > 0
                    THEN ROUND(已完成 / 网格员人数, 2)
                    ELSE 0
                END
            """
        )
        await cur.execute(
            f"SELECT COUNT(*) FROM {_quoted(stage_name)} "
            "WHERE 数据总数 <> 未核查 + 已核查 + 已完成"
        )
        if (await cur.fetchone())[0]:
            raise RuntimeError(
                f"{stage_name} 的三列之和不等于数据总数"
            )
        summary_stages[report_date] = stage_name

    return summary_stages


async def _atomic_swap(
    cur,
    snapshots: list[Snapshot],
    stage_tables: dict[tuple[str, str], tuple[str, str]],
    summary_stages: dict[str, str],
) -> list[str]:
    public_to_stage: dict[str, str] = {}
    for snapshot in snapshots:
        builder = BUILDERS[snapshot.parser_type]
        inspector_public = (
            f"{snapshot.report_date}_daily_"
            f"{builder.table_suffix}_inspector"
        )
        community_public = (
            f"{snapshot.report_date}_daily_"
            f"{builder.table_suffix}_community"
        )
        inspector_stage, community_stage = stage_tables[
            (snapshot.report_date, snapshot.parser_type)
        ]
        public_to_stage[inspector_public] = inspector_stage
        public_to_stage[community_public] = community_stage
    for report_date, stage_name in summary_stages.items():
        public_to_stage[f"{report_date}_daily_summary"] = stage_name

    rename_parts = []
    for public_name, stage_name in sorted(public_to_stage.items()):
        backup_name = _backup_name(public_name)
        if await _table_exists(cur, backup_name):
            raise RuntimeError(
                f"发现旧版保留表 {backup_name}，疑似已完成过回算，"
                "本次停止"
            )
        if await _table_exists(cur, public_name):
            rename_parts.append(
                f"{_quoted(public_name)} TO {_quoted(backup_name)}"
            )
        rename_parts.append(
            f"{_quoted(stage_name)} TO {_quoted(public_name)}"
        )

    if not rename_parts:
        raise RuntimeError("没有可替换的历史日报表")
    await cur.execute("RENAME TABLE " + ", ".join(rename_parts))

    for snapshot in snapshots:
        builder = BUILDERS[snapshot.parser_type]
        for suffix in ("inspector", "community"):
            public_name = (
                f"{snapshot.report_date}_daily_"
                f"{builder.table_suffix}_{suffix}"
            )
            await cur.execute(
                "INSERT INTO _daily_report_meta "
                "(table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, %s, 'backfill') "
                "ON DUPLICATE KEY UPDATE "
                "generation_method='backfill', generated_at=NOW()",
                (
                    public_name,
                    snapshot.report_date,
                    snapshot.parser_type,
                ),
            )

    for report_date in summary_stages:
        public_name = f"{report_date}_daily_summary"
        await cur.execute(
            "INSERT INTO _daily_report_meta "
            "(table_name, report_date, parser_type, generation_method) "
            "VALUES (%s, %s, '总汇总表', 'backfill') "
            "ON DUPLICATE KEY UPDATE "
            "generation_method='backfill', generated_at=NOW()",
            (public_name, report_date),
        )
    return sorted(public_to_stage)


async def _apply(cur, snapshots: list[Snapshot]) -> dict[str, Any]:
    await _ensure_ledger_tables(cur)
    expected_public_tables = []
    for snapshot in snapshots:
        builder = BUILDERS[snapshot.parser_type]
        expected_public_tables.extend(
            [
                (
                    f"{snapshot.report_date}_daily_"
                    f"{builder.table_suffix}_inspector"
                ),
                (
                    f"{snapshot.report_date}_daily_"
                    f"{builder.table_suffix}_community"
                ),
            ]
        )
    expected_public_tables.extend(
        f"{report_date}_daily_summary"
        for report_date in {
            snapshot.report_date for snapshot in snapshots
        }
    )
    existing_backups = [
        _backup_name(public_name)
        for public_name in expected_public_tables
        if await _table_exists(cur, _backup_name(public_name))
    ]
    if existing_backups:
        raise RuntimeError(
            "发现旧版保留表，疑似已完成过回算，本次停止："
            + "、".join(sorted(existing_backups)[:5])
        )

    await cur.execute("SELECT COUNT(*) FROM _daily_task_ledger")
    existing_ledger_rows = int((await cur.fetchone())[0] or 0)
    await cur.execute("DELETE FROM _daily_task_ledger_runs")
    await cur.execute("DELETE FROM _daily_task_ledger")

    stage_tables = await _create_stage_reports(cur, snapshots)
    summary_stages = await _create_stage_summaries(
        cur,
        snapshots,
        stage_tables,
    )
    replaced_tables = await _atomic_swap(
        cur,
        snapshots,
        stage_tables,
        summary_stages,
    )

    await cur.execute(
        "SELECT COUNT(*), COUNT(DISTINCT "
        "CONCAT(report_date, '|', parser_type, '|', row_key)) "
        "FROM _daily_task_ledger"
    )
    total_rows, distinct_rows = await cur.fetchone()
    if int(total_rows) != int(distinct_rows):
        raise RuntimeError("任务流水唯一性验证失败")

    return {
        "existing_ledger_rows_rebuilt": existing_ledger_rows,
        "snapshot_count": len(snapshots),
        "ledger_rows": int(total_rows),
        "replaced_table_count": len(replaced_tables),
        "retained_backup_table_count": sum(
            1
            for public_name in replaced_tables
            if await _table_exists(cur, _backup_name(public_name))
        ),
    }


async def run(apply: bool) -> None:
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DAILY_REPORT_DB,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            snapshots, findings = await _audit_snapshots(cur)
            blocking_findings = [
                finding
                for finding in findings
                if not finding.startswith("快照断档（允许")
            ]
            result = {
                "mode": "apply" if apply else "audit",
                "database_changed": False,
                "snapshot_count": len(snapshots),
                "date_range": (
                    {
                        "start": min(
                            snapshot.report_date
                            for snapshot in snapshots
                        ),
                        "end": max(
                            snapshot.report_date
                            for snapshot in snapshots
                        ),
                    }
                    if snapshots
                    else None
                ),
                "findings": findings,
            }
            print(json.dumps(result, ensure_ascii=False))
            if blocking_findings:
                raise RuntimeError(
                    "历史快照审计未通过，未替换任何历史日报"
                )
            if not apply:
                return

            applied = await _apply(cur, snapshots)
            print(
                json.dumps(
                    {
                        "mode": "apply",
                        "database_changed": True,
                        **applied,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行回算和原子替换；不传时只审计",
    )
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
