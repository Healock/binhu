"""按走访业务日期汇总人员、社区和在岗人日。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable

from services.personnel_attendance import (
    allocate_person_days,
    get_attendance_context,
)
from services.personnel_positions import (
    VISIT_POSITION_CONFIG_KEY,
    filter_person_rows,
    get_configured_positions,
    get_known_personnel_positions,
)
from services.report_members import get_active_members

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
    "网格员人数",
    "在岗人日",
    "人均日走访户数",
    "新增",
    "变更",
    "注销",
    "总变动数",
    "人均日变动数",
    "户均变动数",
    "星级评定数",
    "星级评定率",
]


def _int(value: Any) -> int:
    return int(value or 0)


def _round_ratio(
    numerator: int | Decimal,
    denominator: int | float | Decimal,
) -> float:
    denominator_value = Decimal(str(denominator or 0))
    if denominator_value <= 0:
        return 0.0
    return float(
        (Decimal(str(numerator or 0)) / denominator_value).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _round_person_days(value: int | float | Decimal) -> float:
    return float(
        Decimal(str(value or 0)).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _balanced_person_day_display(
    allocations: dict[str, Decimal],
) -> dict[str, Decimal]:
    """保留一位小数，同时让各社区显示值之和等于总计显示值。"""
    if not allocations:
        return {}
    unit = Decimal("0.1")
    displayed = {
        community: value.quantize(unit, rounding=ROUND_FLOOR)
        for community, value in allocations.items()
    }
    target = sum(allocations.values(), Decimal(0)).quantize(
        unit,
        rounding=ROUND_HALF_UP,
    )
    remaining_units = int(
        (target - sum(displayed.values(), Decimal(0))) / unit
    )
    ranked = sorted(
        allocations,
        key=lambda community: (
            allocations[community] - displayed[community],
            community,
        ),
        reverse=True,
    )
    for community in ranked[:remaining_units]:
        displayed[community] += unit
    return displayed


def _rating_rate(ratings: int, visits: int) -> float:
    if visits <= 0:
        return 0.0
    return float(
        (Decimal(ratings) / Decimal(visits)).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
    )


def _inspector_row(
    community: str,
    name: str,
    values: dict[str, int],
) -> dict[str, Any]:
    visits = values["visits"]
    total_changes = (
        values["added"] + values["changed"] + values["cancelled"]
    )
    row = {
        "社区": community,
        "姓名": name,
        "走访户数": visits,
        "新增": values["added"],
        "变更": values["changed"],
        "注销": values["cancelled"],
        "总变动数": total_changes,
        "户均变动数": _round_ratio(total_changes, visits),
        "星级评定数": values["ratings"],
        "星级评定率": _rating_rate(values["ratings"], visits),
    }
    return {column: row[column] for column in INSPECTOR_COLUMNS}


def _community_row(
    community: str,
    values: dict[str, int],
    member_count: int,
    person_days: Decimal,
    displayed_person_days: Decimal,
    *,
    attendance_complete: bool,
) -> dict[str, Any]:
    visits = values["visits"]
    total_changes = (
        values["added"] + values["changed"] + values["cancelled"]
    )
    row = {
        "社区": community,
        "走访户数": visits,
        "网格员人数": int(member_count or 0),
        "在岗人日": _round_person_days(displayed_person_days),
        "人均日走访户数": (
            _round_ratio(visits, person_days)
            if attendance_complete
            else None
        ),
        "新增": values["added"],
        "变更": values["changed"],
        "注销": values["cancelled"],
        "总变动数": total_changes,
        "人均日变动数": (
            _round_ratio(total_changes, person_days)
            if attendance_complete
            else None
        ),
        "户均变动数": _round_ratio(total_changes, visits),
        "星级评定数": values["ratings"],
        "星级评定率": _rating_rate(values["ratings"], visits),
    }
    result = {column: row[column] for column in COMMUNITY_COLUMNS}
    result["_person_days_exact"] = float(person_days)
    return result


def _build_total(
    rows: Iterable[dict[str, Any]],
    *,
    inspector: bool,
    attendance_complete: bool = True,
) -> dict[str, Any]:
    materialized = list(rows)
    visits = sum(_int(row.get("走访户数")) for row in materialized)
    added = sum(_int(row.get("新增")) for row in materialized)
    changed = sum(_int(row.get("变更")) for row in materialized)
    cancelled = sum(_int(row.get("注销")) for row in materialized)
    ratings = sum(_int(row.get("星级评定数")) for row in materialized)
    total_changes = added + changed + cancelled
    total: dict[str, Any] = {
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
    person_days = sum(
        Decimal(str(
            row.get("_person_days_exact", row.get("在岗人日")) or 0
        ))
        for row in materialized
    )
    total["在岗人日"] = _round_person_days(person_days)
    total["网格员人数"] = sum(
        _int(row.get("网格员人数")) for row in materialized
    )
    total["人均日走访户数"] = (
        _round_ratio(visits, person_days)
        if attendance_complete
        else None
    )
    total["人均日变动数"] = (
        _round_ratio(total_changes, person_days)
        if attendance_complete
        else None
    )
    return {column: total[column] for column in COMMUNITY_COLUMNS}


def _build_overview(
    inspector_rows: list[dict[str, Any]],
    community_rows: list[dict[str, Any]],
    person_days: Decimal,
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
        "person_days": _round_person_days(person_days),
        "community_count": len(community_rows),
        "added_count": _int(total["新增"]),
        "changed_count": _int(total["变更"]),
        "cancelled_count": _int(total["注销"]),
        "total_changes": _int(total["总变动数"]),
        "rated_records": ratings,
        "unrated_records": max(visits - ratings, 0),
        "rating_rate": _rating_rate(ratings, visits),
    }


def _empty_values() -> dict[str, int]:
    return {
        "visits": 0,
        "added": 0,
        "changed": 0,
        "cancelled": 0,
        "ratings": 0,
    }


async def get_visit_summary(
    conn,
    start_date: date,
    end_date: date,
    *,
    category: str = VISIT_CATEGORY_RENTAL,
    selected_positions: set[str] | None = None,
    known_positions: dict[str, str] | None = None,
    attendance_context: dict[str, Any] | None = None,
    community_scope: list[str] | str | None = None,
    community_names: list[str] | None = None,
    inspector_scope: str | None = None,
) -> dict[str, Any]:
    """查询闭区间内的走访，并按真实在岗人日计算社区人均值。"""
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

        community_clause = ""
        query_params: tuple[Any, ...] = (start_date, end_date)
        if community_scope is not None:
            allowed = community_names if community_names is not None else (
                community_scope if isinstance(community_scope, list)
                else [community_scope]
            )
            if allowed:
                placeholders = ",".join(["%s"] * len(allowed))
                community_clause = f"AND TRIM(`社区`) IN ({placeholders})"
                query_params = (*query_params, *allowed)
            else:
                community_clause = "AND 1=0"
        inspector_clause = ""
        if inspector_scope is not None:
            inspector_clause = (
                "AND LOWER(TRIM(`操作人`))=LOWER(TRIM(%s))"
            )
            query_params = (*query_params, str(inspector_scope).strip())
        await cur.execute(
            f"""
            SELECT
                `业务日期`,
                COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区'),
                COALESCE(NULLIF(TRIM(`操作人`), ''), '未填写姓名'),
                COUNT(*),
                COALESCE(SUM(`新增`), 0),
                COALESCE(SUM(`变更`), 0),
                COALESCE(SUM(`注销`), 0),
                COUNT(`星级采集时间`)
            FROM t_visit_details
            WHERE `业务日期` BETWEEN %s AND %s
              {community_clause}
              {inspector_clause}
            GROUP BY
                `业务日期`,
                COALESCE(NULLIF(TRIM(`社区`), ''), '未分配社区'),
                COALESCE(NULLIF(TRIM(`操作人`), ''), '未填写姓名')
            ORDER BY 1, 2, 3
            """,
            query_params,
        )
        raw_daily_rows = filter_person_rows(
            await cur.fetchall(),
            name_index=2,
            selected_positions=selected_positions,
            known_positions=known_positions,
            include_unknown=include_unknown,
        )
        if attendance_context is None:
            attendance_context = await get_attendance_context(
                cur,
                start_date=start_date,
                end_date=end_date,
                selected_positions=selected_positions,
                community_scope=(
                    community_scope if isinstance(community_scope, list)
                    else ([community_scope] if community_scope else community_scope)
                ),
                member_names=(
                    [str(inspector_scope).strip()]
                    if inspector_scope is not None
                    else None
                ),
            )
        active_online_members = await get_active_members(
            cur,
            end_date.isoformat(),
        )

    community_member_counts: dict[str, int] = {}
    for community, _name in active_online_members:
        community_member_counts[community] = (
            community_member_counts.get(community, 0) + 1
        )

    inspector_totals: dict[tuple[str, str], dict[str, int]] = {}
    daily_visits: dict[tuple[date, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in raw_daily_rows:
        business_date = row[0]
        community = str(row[1] or "未分配社区")
        name = str(row[2] or "未填写姓名")
        bucket = inspector_totals.setdefault(
            (community, name),
            _empty_values(),
        )
        bucket["visits"] += _int(row[3])
        bucket["added"] += _int(row[4])
        bucket["changed"] += _int(row[5])
        bucket["cancelled"] += _int(row[6])
        bucket["ratings"] += _int(row[7])
        daily_visits[(business_date, name)][community] += _int(row[3])

    inspector_rows = [
        _inspector_row(community, name, values)
        for (community, name), values in inspector_totals.items()
    ]
    attendance = allocate_person_days(
        start_date=start_date,
        end_date=end_date,
        daily_visits={
            key: dict(value)
            for key, value in daily_visits.items()
        },
        context=attendance_context,
        include_unknown=include_unknown,
    )

    community_totals: dict[str, dict[str, int]] = {}
    for (community, _name), values in inspector_totals.items():
        bucket = community_totals.setdefault(community, _empty_values())
        for key in bucket:
            bucket[key] += values[key]
    all_communities = dict.fromkeys([
        *community_totals,
        *attendance["community_person_days"],
    ])
    displayed_person_days = _balanced_person_day_display(
        attendance["community_person_days"]
    )
    community_rows = [
        _community_row(
            community,
            community_totals.get(community, _empty_values()),
            community_member_counts.get(community, 0),
            attendance["community_person_days"].get(
                community,
                Decimal(0),
            ),
            displayed_person_days.get(community, Decimal(0)),
            attendance_complete=attendance["complete"],
        )
        for community in all_communities
    ]

    attendance_payload = {
        "complete": attendance["complete"],
        "person_days": _round_person_days(
            attendance["total_person_days"],
        ),
        "missing_week_starts": [
            value.isoformat()
            for value in attendance["missing_week_starts"]
        ],
        "history_started_on": (
            attendance["history_started_on"].isoformat()
            if attendance["history_started_on"]
            else None
        ),
        "legacy_history_incomplete": attendance[
            "legacy_history_incomplete"
        ],
        "worked_while_off": attendance["worked_while_off"],
        "unknown_participant_days": attendance[
            "unknown_participant_days"
        ],
    }
    result = {
        "category": category,
        "category_label": VISIT_CATEGORY_LABELS[category],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "attendance": attendance_payload,
        "overview": _build_overview(
            inspector_rows,
            community_rows,
            attendance["total_person_days"],
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
                attendance_complete=attendance["complete"],
            ),
        },
    }
    if community_scope == "" or community_scope == []:
        result["scope_message"] = "当前账号尚未分配社区部门，暂无业务数据"
    return result
