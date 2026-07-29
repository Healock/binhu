"""按走访业务日期汇总网格员和社区工作量。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from services.personnel_positions import (
    VISIT_POSITION_CONFIG_KEY,
    filter_person_rows,
    get_configured_positions,
    get_known_personnel_positions,
)

VISIT_CATEGORY_RENTAL = "rental"
VISIT_CATEGORY_SELF_OWNED = "self_owned"
VISIT_CATEGORY_LABELS = {
    VISIT_CATEGORY_RENTAL: "出租房",
    VISIT_CATEGORY_SELF_OWNED: "自购房",
}

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


def _build_overview(
    inspector_rows: list[dict[str, Any]],
    community_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    total = _build_total(inspector_rows, inspector=True)
    visits = _int(total["走访户数"])
    ratings = _int(total["星级评定数"])
    return {
        "visit_records": visits,
        "participant_count": len({
            str(row.get("姓名") or "未填写姓名")
            for row in inspector_rows
        }),
        "community_count": len(community_rows),
        "added_count": _int(total["新增"]),
        "changed_count": _int(total["变更"]),
        "cancelled_count": _int(total["注销"]),
        "total_changes": _int(total["总变动数"]),
        "rated_records": ratings,
        "unrated_records": max(visits - ratings, 0),
        "rating_rate": _rating_rate(ratings, visits),
    }


async def get_visit_summary(
    conn,
    start_date: date,
    end_date: date,
    *,
    category: str = VISIT_CATEGORY_RENTAL,
    selected_positions: set[str] | None = None,
    known_positions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """查询闭区间内的走访，并分别按实际走访社区和网格员汇总。"""
    if category not in VISIT_CATEGORY_LABELS:
        raise ValueError("不支持的走访汇总类型")
    async with conn.cursor() as cur:
        if known_positions is None:
            known_positions = await get_known_personnel_positions(cur)
        if selected_positions is None:
            if category == VISIT_CATEGORY_SELF_OWNED:
                selected_positions = {"自购房"}
            else:
                selected_positions = set(await get_configured_positions(
                    cur,
                    VISIT_POSITION_CONFIG_KEY,
                ))
        if category == VISIT_CATEGORY_RENTAL:
            selected_positions = selected_positions - {"自购房"}
            include_unknown = True
        else:
            selected_positions = {"自购房"}
            include_unknown = False
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
        raw_inspector_rows = filter_person_rows(
            await cur.fetchall(),
            name_index=1,
            selected_positions=selected_positions,
            known_positions=known_positions,
            include_unknown=include_unknown,
        )
        inspector_rows = [
            _build_row(row, inspector=True)
            for row in raw_inspector_rows
        ]

        community_totals: dict[str, dict[str, Any]] = {}
        for row in raw_inspector_rows:
            community = str(row[0] or "未分配社区")
            bucket = community_totals.setdefault(
                community,
                {
                    "members": set(),
                    "visits": 0,
                    "added": 0,
                    "changed": 0,
                    "cancelled": 0,
                    "ratings": 0,
                },
            )
            bucket["members"].add(str(row[1] or "未填写姓名"))
            bucket["visits"] += _int(row[2])
            bucket["added"] += _int(row[3])
            bucket["changed"] += _int(row[4])
            bucket["cancelled"] += _int(row[5])
            bucket["ratings"] += _int(row[6])

        community_rows = [
            _build_row(
                (
                    community,
                    totals["visits"],
                    len(totals["members"]),
                    totals["added"],
                    totals["changed"],
                    totals["cancelled"],
                    totals["ratings"],
                ),
                inspector=False,
            )
            for community, totals in sorted(community_totals.items())
        ]

    distinct_members = len({
        str(row.get("姓名") or "未填写姓名")
        for row in inspector_rows
    })
    return {
        "category": category,
        "category_label": VISIT_CATEGORY_LABELS[category],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "overview": _build_overview(
            inspector_rows,
            community_rows,
        ),
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
