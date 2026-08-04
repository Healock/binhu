"""按历史同步快照重建有效工作量流水和日报。

本模块只提供可测试的服务函数，不注册网页或公共 API。生产执行前必须先预览、
备份 ``daily_report``，再由放在 ``scratch/`` 的一次性包装显式调用执行函数。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from database import db_manager
from services.report_builders import BUILDERS
from services.report_builders.summary import build_summary


def _validate_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD") from exc


def _validate_parser_types(parser_types: list[str] | None) -> list[str]:
    if parser_types is None:
        return list(BUILDERS)
    requested = list(dict.fromkeys(parser_types))
    unknown = [item for item in requested if item not in BUILDERS]
    if unknown:
        raise ValueError(f"不支持的日报类型：{'、'.join(unknown)}")
    return requested


async def preview_effective_workload_rebuild(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    parser_types: list[str] | None = None,
) -> dict[str, Any]:
    """只读列出将被重建的日期、业务及现有流水规模。"""
    start = _validate_date(start_date, "开始日期")
    end = _validate_date(end_date, "结束日期")
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    selected = _validate_parser_types(parser_types)
    snapshot_types = [f"{item}_snapshot" for item in selected]
    placeholders = ",".join(["%s"] * len(snapshot_types))
    where = [f"parser_type IN ({placeholders})", "generation_method='snapshot'"]
    params: list[Any] = list(snapshot_types)
    if start:
        where.append("report_date >= %s")
        params.append(start)
    if end:
        where.append("report_date <= %s")
        params.append(end)

    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT report_date, parser_type, table_name "
                "FROM _daily_report_meta WHERE " + " AND ".join(where) +
                " ORDER BY report_date, parser_type",
                params,
            )
            targets = []
            for report_date, snapshot_type, table_name in await cur.fetchall():
                parser_type = str(snapshot_type)[:-9]
                builder = BUILDERS.get(parser_type)
                expected_table = (
                    f"{report_date.isoformat()}_snapshot_{builder.table_suffix}"
                    if builder and hasattr(report_date, "isoformat")
                    else f"{report_date}_snapshot_{builder.table_suffix}" if builder else ""
                )
                if builder and str(table_name) == expected_table:
                    targets.append({
                        "report_date": (
                            report_date.isoformat()
                            if hasattr(report_date, "isoformat")
                            else str(report_date)
                        ),
                        "parser_type": parser_type,
                        "snapshot_table": str(table_name),
                    })

            if targets:
                dates = sorted({item["report_date"] for item in targets})
                types = sorted({item["parser_type"] for item in targets})
                date_placeholders = ",".join(["%s"] * len(dates))
                type_placeholders = ",".join(["%s"] * len(types))
                await cur.execute(
                    "SELECT COUNT(*), COALESCE(SUM(effective_workload), 0) "
                    "FROM _daily_task_ledger "
                    f"WHERE report_date IN ({date_placeholders}) "
                    f"AND parser_type IN ({type_placeholders})",
                    (*dates, *types),
                )
                ledger_rows, workload = await cur.fetchone()
            else:
                dates = []
                ledger_rows, workload = 0, 0
    finally:
        pool.release(conn)

    return {
        "targets": targets,
        "date_count": len(dates),
        "snapshot_count": len(targets),
        "existing_ledger_rows": int(ledger_rows or 0),
        "existing_effective_workload": int(workload or 0),
    }


async def rebuild_effective_workload_history(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    parser_types: list[str] | None = None,
) -> dict[str, Any]:
    """按日期升序幂等重建流水、分汇总及总汇总。"""
    preview = await preview_effective_workload_rebuild(
        start_date=start_date,
        end_date=end_date,
        parser_types=parser_types,
    )
    grouped: dict[str, list[str]] = defaultdict(list)
    for target in preview["targets"]:
        grouped[target["report_date"]].append(target["parser_type"])

    rebuilt: list[dict[str, Any]] = []
    for report_date in sorted(grouped):
        subreports = []
        for parser_type in grouped[report_date]:
            result = await BUILDERS[parser_type].build(
                report_date,
                generation_method="workload_backfill",
                reset_ledger=True,
            )
            if result.get("implemented") is False:
                raise RuntimeError(
                    f"{report_date} {parser_type} 回算失败："
                    f"{result.get('message') or '未知原因'}"
                )
            subreports.append({"parser_type": parser_type, **result})
        summary = await build_summary(
            report_date,
            generation_method="workload_backfill",
        )
        if summary.get("implemented") is False:
            raise RuntimeError(
                f"{report_date} 总汇总回算失败："
                f"{summary.get('message') or '未知原因'}"
            )
        rebuilt.append({
            "report_date": report_date,
            "subreports": subreports,
            "summary": summary,
        })
    return {"preview": preview, "rebuilt": rebuilt}
