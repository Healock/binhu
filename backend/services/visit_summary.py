"""按走访业务日期汇总网格员和社区工作量。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable


INSPECTOR_COLUMNS = [
    "社区",
    "姓名",
    "走访户数",
    "新增",
    "变更",
    "注销",
    "总变动数",
    "户均变动数",
    "星级评定数",
    "星级评定率",
]

COMMUNITY_COLUMNS = [
    "社区",
    "走访户数",
    "人均走访户数",
    "新增",
    "变更",
    "注销",
    "总变动数",
    "人均变动数",
    "户均变动数",
    "星级评定数",
    "星级评定率",
]


def _int(value: Any) -> int:
    return int(value or 0)


def _round_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _rating_rate(ratings: int, visits: int) -> float:
    if visits <= 0:
        return 0.0
    return float(
        (Decimal(ratings) / Decimal(visits)).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
    )


def _build_row(raw: tuple[Any, ...], *, inspector: bool) -> dict[str, Any]:
    member_count = 0 if inspector else _int(raw[2])
    visits = _int(raw[2] if inspector else raw[1])
    added = _int(raw[3])
    changed = _int(raw[4])
    cancelled = _int(raw[5])
    ratings = _int(raw[6])
    total_changes = added + changed + cancelled
    row = {
        "社区": str(raw[0] or "未分配社区"),
        "走访户数": visits,
        "新增": added,
        "变更": changed,
        "注销": cancelled,
        "总变动数": total_changes,
        "户均变动数": _round_ratio(total_changes, visits),
        "星级评定数": ratings,
        "星级评定率": _rating_rate(ratings, visits),
    }
    if inspector:
        row["姓名"] = str(raw[1] or "未填写姓名")
        return {column: row[column] for column in INSPECTOR_COLUMNS}
    row["人均走访户数"] = _round_ratio(visits, member_count)
    row["人均变动数"] = _round_ratio(total_changes, member_count)
    return {column: row[column] for column in COMMUNITY_COLUMNS}


def _build_total(
    rows: Iterable[dict[str, Any]],
    *,
    inspector: bool,
    member_count: int = 0,
) -> dict[str, Any]:
    materialized = list(rows)
    visits = sum(_int(row.get("走访户数")) for row in materialized)
    added = sum(_int(row.get("新增")) for row in materialized)
    changed = sum(_int(row.get("变更")) for row in materialized)
    cancelled = sum(_int(row.get("注销")) for row in materialized)
    ratings = sum(_int(row.get("星级评定数")) for row in materialized)
    total_changes = added + changed + cancelled
    total = {
        "社区": "总计",
        "走访户数": visits,
        "新增": added,
        "变更": changed,
        "注销": cancelled,
        "总变动数": total_changes,
        "户均变动数": _round_ratio(total_changes, visits),
        "星级评定数": ratings,
        "星级评定率": _rating_rate(ratings, visits),
    }
    if inspector:
        total["姓名"] = ""
        return {column: total[column] for column in INSPECTOR_COLUMNS}
    total["人均走访户数"] = _round_ratio(visits, member_count)
    total["人均变动数"] = _round_ratio(
        total_changes,
        member_count,
    )
    return {column: total[column] for column in COMMUNITY_COLUMNS}


async def get_visit_summary(
    conn,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """查询闭区间内的走访，并分别按实际走访社区和网格员汇总。"""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区'),
                COALESCE(NULLIF(TRIM(`操作人`), ''), '未填写姓名'),
                COUNT(*),
                COALESCE(SUM(`新增`), 0),
                COALESCE(SUM(`变更`), 0),
                COALESCE(SUM(`注销`), 0),
                COUNT(`星级采集时间`)
            FROM t_visit_details
            WHERE `业务日期` BETWEEN %s AND %s
            GROUP BY
                COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区'),
                COALESCE(NULLIF(TRIM(`操作人`), ''), '未填写姓名')
            ORDER BY 1, 2
            """,
            (start_date, end_date),
        )
        inspector_rows = [
            _build_row(row, inspector=True)
            for row in await cur.fetchall()
        ]

        await cur.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区'),
                COUNT(*),
                COUNT(DISTINCT COALESCE(
                    NULLIF(TRIM(`操作人`), ''),
                    '未填写姓名'
                )),
                COALESCE(SUM(`新增`), 0),
                COALESCE(SUM(`变更`), 0),
                COALESCE(SUM(`注销`), 0),
                COUNT(`星级采集时间`)
            FROM t_visit_details
            WHERE `业务日期` BETWEEN %s AND %s
            GROUP BY COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区')
            ORDER BY 1
            """,
            (start_date, end_date),
        )
        community_rows = [
            _build_row(row, inspector=False)
            for row in await cur.fetchall()
        ]

    distinct_members = len({
        str(row.get("姓名") or "未填写姓名")
        for row in inspector_rows
    })
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "inspector": {
            "columns": INSPECTOR_COLUMNS,
            "data": inspector_rows,
            "summary": _build_total(inspector_rows, inspector=True),
        },
        "community": {
            "columns": COMMUNITY_COLUMNS,
            "data": community_rows,
            "summary": _build_total(
                community_rows,
                inspector=False,
                member_count=distinct_members,
            ),
        },
    }
