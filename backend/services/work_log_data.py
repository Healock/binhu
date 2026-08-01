"""工作日志日报的系统数据快照。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from services.report_builders.summary import get_summary
from services.report_view import project_report_payload
from services.personnel_attendance import (
    get_attendance_context,
    is_member_on_duty,
)
from services.personnel_positions import ONLINE_SUMMARY_POSITIONS
from services.visit_summary import (
    VISIT_CATEGORY_RENTAL,
    VISIT_CATEGORY_SELF_OWNED,
    get_visit_summary,
)


def _number(value: Any) -> int:
    return int(value or 0)


def _decimal(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _percent(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(
        (Decimal(str(value)) * Decimal(100)).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


async def _community_names(conn) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name FROM _communities "
            "WHERE name IS NOT NULL AND TRIM(name) <> '' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
    return [str(row[0]).strip() for row in rows if str(row[0]).strip()]


async def _community_officers(conn) -> dict[str, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name, police_officers FROM _communities "
            "WHERE name IS NOT NULL AND TRIM(name) <> '' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
    result: dict[str, str] = {}
    for community, raw_officers in rows:
        officers = raw_officers
        if isinstance(raw_officers, str):
            try:
                officers = json.loads(raw_officers)
            except (TypeError, ValueError, json.JSONDecodeError):
                officers = []
        if not isinstance(officers, list):
            officers = []
        names = [
            str(name).strip()
            for name in officers
            if str(name).strip()
        ]
        result[str(community).strip()] = "、".join(names)
    return result


async def _community_grid_member_counts(
    conn,
    business_date: date,
    communities: list[str],
) -> dict:
    async with conn.cursor() as cur:
        positions = set(ONLINE_SUMMARY_POSITIONS)
        context = await get_attendance_context(
            cur,
            start_date=business_date,
            end_date=business_date,
            selected_positions=positions,
        )

    if context["missing_week_starts"]:
        return {
            "available": False,
            "message": "当天双休日备勤尚未排完，网格员数未自动填写",
            "counts": {},
        }

    counts = {community: 0 for community in communities}
    for member in context["members"].values():
        if not is_member_on_duty(member, business_date, context):
            continue
        community = str(member.get("community") or "未分配社区")
        counts[community] = counts.get(community, 0) + 1

    message = ""
    if context["legacy_history_incomplete"]:
        message = "所选日期早于完整出勤历史，自动人数仅供参考"
    return {
        "available": True,
        "message": message,
        "counts": counts,
    }


async def _online_summary_snapshot(
    business_date: date,
    community_officers: dict[str, str] | None = None,
    community_grid_member_counts: dict[str, int] | None = None,
) -> dict:
    community_officers = community_officers or {}
    generated_counts_available = community_grid_member_counts is not None
    community_grid_member_counts = community_grid_member_counts or {}
    result = await get_summary(business_date.isoformat())
    if not result.get("exists"):
        return {
            "available": False,
            "message": result.get("message") or "该日期没有可用的在线总汇总表",
            "values": {},
        }
    projected = project_report_payload(result, "two")
    rows = (projected.get("community") or {}).get("data") or []
    table_rows = []
    for row in rows:
        community = str(row.get("社区") or "")
        table_rows.append({
            "responsibility_area": community,
            "community_officer": community_officers.get(
                community,
                "",
            ),
            "grid_member_count": (
                community_grid_member_counts.get(community, "")
                if generated_counts_available
                else _number(row.get("网格员人数"))
            ),
            "total": _number(row.get("数据总数")),
            "unchecked": _number(row.get("未核查")),
            "checked": _number(row.get("已核查")),
            "completion_rate": _percent(row.get("核查完成率")),
            "unable": _number(row.get("无法见底数")),
            "ground_rate": _percent(row.get("核查见底率")),
            "average_checked": _decimal(row.get("当日人均核查数")),
        })
    if not table_rows:
        return {
            "available": False,
            "message": "该日期的在线总汇总表没有社区数据",
            "values": {},
        }
    return {
        "available": True,
        "message": "",
        "values": {"flow.instruction_table": table_rows},
    }


async def _rental_snapshot(
    conn,
    business_date: date,
    community_officers: dict[str, str] | None = None,
    community_grid_member_counts: dict[str, int] | None = None,
) -> dict:
    community_officers = community_officers or {}
    generated_counts_available = community_grid_member_counts is not None
    community_grid_member_counts = community_grid_member_counts or {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM t_visit_details WHERE `业务日期`=%s",
            (business_date,),
        )
        row = await cur.fetchone()
    if not row or _number(row[0]) == 0:
        return {
            "available": False,
            "message": "该日期没有可用的出租房走访数据",
            "values": {},
        }

    result = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_RENTAL,
    )
    rows = (result.get("community") or {}).get("data") or []
    table_rows = []
    for row in rows:
        community = str(row.get("社区") or "")
        visits = _number(row.get("走访户数"))
        added = _number(row.get("新增"))
        changed = _number(row.get("变更"))
        cancelled = _number(row.get("注销"))
        table_rows.append({
            "responsibility_area": community,
            "community_officer": community_officers.get(
                community,
                "",
            ),
            "grid_member_count": (
                community_grid_member_counts.get(community, "")
                if generated_counts_available
                else _decimal(row.get("在岗人日"))
            ),
            "visits": visits,
            "average_visits": _decimal(row.get("人均日走访户数")),
            "added": added,
            "changed": changed,
            "cancelled": cancelled,
            "total_changes": added + changed + cancelled,
            "average_changes": _decimal(row.get("人均日变动数")),
            "household_changes": _decimal(row.get("户均变动数")),
            "rated": _number(row.get("星级评定数")),
            "rating_rate": _percent(row.get("星级评定率")),
        })

    attendance = result.get("attendance") or {}
    message = ""
    if not attendance.get("complete", True):
        message = "该日期出勤资料不完整，表格中的人均值可能为空"
    return {
        "available": bool(table_rows),
        "message": message if table_rows else "该日期没有出租房社区汇总数据",
        "values": (
            {"rental.visit_table": table_rows}
            if table_rows
            else {}
        ),
    }


async def _self_owned_snapshot(conn, business_date: date) -> dict:
    result = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_SELF_OWNED,
    )
    rows = (result.get("inspector") or {}).get("data") or []
    table_rows = [
        {
            "grid_member": str(row.get("姓名") or ""),
            "visits": _number(row.get("走访户数")),
            "changed": _number(row.get("变更")),
            "cancelled": _number(row.get("注销")),
        }
        for row in rows
        if str(row.get("姓名") or "").strip()
    ]
    return {
        "available": bool(table_rows),
        "message": (
            ""
            if table_rows
            else "该日期没有可用的自购房走访数据"
        ),
        "values": (
            {"self_owned.visit_table": table_rows}
            if table_rows
            else {}
        ),
    }


async def build_system_snapshot(conn, business_date: date) -> dict:
    """读取一次系统数据，返回可长期保存的普通 JSON 对象。"""
    communities = await _community_names(conn)
    community_officers = await _community_officers(conn)
    grid_members = await _community_grid_member_counts(
        conn,
        business_date,
        communities,
    )
    community_grid_member_counts = (
        grid_members["counts"]
        if grid_members["available"]
        else None
    )
    online = await _online_summary_snapshot(
        business_date,
        community_officers,
        community_grid_member_counts,
    )
    rental = await _rental_snapshot(
        conn,
        business_date,
        community_officers,
        community_grid_member_counts,
    )
    self_owned = await _self_owned_snapshot(conn, business_date)
    issue_date = business_date + timedelta(days=1)
    return {
        "business_date": business_date.isoformat(),
        "issue_date": issue_date.isoformat(),
        "month": business_date.month,
        "filename_prefix": business_date.strftime("%m%d"),
        "communities": communities,
        "community_officers": community_officers,
        "community_grid_member_counts": grid_members["counts"],
        "values": {
            "meta.year": business_date.year,
            "meta.month": business_date.month,
            "meta.day": business_date.day,
            **online["values"],
            **rental["values"],
            **self_owned["values"],
        },
        "sources": {
            "online_summary": {
                "label": "在线数据总汇总表",
                "available": online["available"],
                "message": online["message"],
            },
            "rental_visit": {
                "label": "出租房走访",
                "available": rental["available"],
                "message": rental["message"],
            },
            "self_owned_visit": {
                "label": "自购房走访",
                "available": self_owned["available"],
                "message": self_owned["message"],
            },
            "grid_member_counts": {
                "label": "网格员人数",
                "available": grid_members["available"],
                "message": grid_members["message"],
            },
        },
    }
