"""人员出勤、请假历史与双休日备勤计算。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from services.personnel_positions import (
    DEFAULT_SUMMARY_POSITIONS,
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    get_configured_positions,
)
from services.business_time import get_business_date
from services.attendance_schedule import load_daily, resolve_day, lock_members, record_change, mutation, table

DEFAULT_WEEKEND_DUTY_POSITIONS = set(DEFAULT_SUMMARY_POSITIONS)


def normalize_week_start(value: date) -> date:
    """把任意日期归到所在周的周一。"""
    return value - timedelta(days=value.weekday())


def weekend_dates(week_start: date) -> tuple[date, date]:
    monday = normalize_week_start(week_start)
    return monday + timedelta(days=5), monday + timedelta(days=6)


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def required_week_starts(
    start_date: date,
    end_date: date,
    business_today: date,
) -> list[date]:
    """返回截至业务当天已经发生的双休日所在周。"""
    return sorted({
        normalize_week_start(target_date)
        for target_date in iter_dates(start_date, end_date)
        if target_date.weekday() >= 5 and target_date <= business_today
    })


def period_covers(
    target_date: date,
    periods: Iterable[dict[str, Any]],
) -> bool:
    for period in periods:
        if not period.get("is_active", True):
            continue
        start = period["start_date"]
        end = period.get("end_date")
        if start <= target_date and (end is None or target_date <= end):
            return True
    return False


def duty_label(duty_date: date | None, saturday: date, sunday: date) -> str | None:
    if duty_date == saturday:
        return "saturday"
    if duty_date == sunday:
        return "sunday"
    return None


async def list_attendance_periods(
    cur,
    *,
    start_date: date,
    end_date: date,
    member_ids: Iterable[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    params: list[Any] = [end_date, start_date]
    member_filter = ""
    materialized_ids = list(member_ids) if member_ids is not None else None
    if materialized_ids == []:
        return {}
    if materialized_ids:
        placeholders = ", ".join(["%s"] * len(materialized_ids))
        member_filter = f" AND member_id IN ({placeholders})"
        params.extend(materialized_ids)
    await cur.execute(
        f"""
        SELECT member_id, absence_type, start_date, end_date,
               reason, source, is_active
        FROM {table('_personnel_attendance_history')}
        WHERE is_active=1
          AND start_date <= %s
          AND (end_date IS NULL OR end_date >= %s)
        """
        + member_filter
        + " ORDER BY member_id, start_date, id",
        params,
    )
    periods: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in await cur.fetchall():
        periods[int(row[0])].append(
            {
                "absence_type": row[1],
                "start_date": row[2],
                "end_date": row[3],
                "reason": row[4] or "",
                "source": row[5] or "manual",
                "is_active": bool(row[6]),
            }
        )
    return dict(periods)


async def get_weekend_board(cur, requested_date: date) -> dict[str, Any]:
    week_start = normalize_week_start(requested_date)
    saturday, sunday = weekend_dates(week_start)
    duty_positions = await get_configured_positions(
        cur,
        WEEKEND_DUTY_POSITION_CONFIG_KEY,
    )
    placeholders = ", ".join(["%s"] * len(duty_positions))
    await cur.execute(
        f"""
        SELECT member.id, member.name,
               GROUP_CONCAT(DISTINCT community.name
                   ORDER BY link.sort_order, community.name SEPARATOR '、'),
               member.position
        FROM {table('_grid_members')} AS member
        LEFT JOIN {table('_grid_member_department_links')} AS link
          ON link.member_id=member.id
        LEFT JOIN {table('_departments')} AS department
          ON department.id=link.department_id
        LEFT JOIN {table('_communities')} AS community
          ON community.id=department.community_id
        WHERE member.position IN ({placeholders})
        GROUP BY member.id, member.name, member.position, member.community
        ORDER BY COALESCE(GROUP_CONCAT(DISTINCT community.name
                     ORDER BY link.sort_order, community.name SEPARATOR '、'),
                 member.community),
                 member.position, member.name
        """,
        duty_positions,
    )
    members = [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "community": str(row[2] or "未分配社区"),
            "position": str(row[3]),
        }
        for row in await cur.fetchall()
    ]
    member_ids = [member["id"] for member in members]
    periods = await list_attendance_periods(
        cur,
        start_date=week_start,
        end_date=sunday,
        member_ids=member_ids,
    )

    daily = await load_daily(cur, member_ids, saturday - timedelta(days=7), sunday)
    assignments = {
        m: [label for label, day in (("saturday", saturday), ("sunday", sunday)) if daily.get((m, day)) == "work"]
        for m in member_ids
    }
    recorded_member_ids = {m for m in member_ids if all((m, d) in daily for d in (saturday, sunday))}
    previous_assignments = {
        m: [label for label, day in (("saturday", saturday-timedelta(days=7)), ("sunday", sunday-timedelta(days=7))) if daily.get((m, day)) == "work"]
        for m in member_ids
    }
    versions = {}
    if member_ids:
        await cur.execute(f"SELECT id,attendance_revision FROM {table('_grid_members')} WHERE id IN ({','.join(['%s']*len(member_ids))})", member_ids)
        versions = {int(m):int(v) for m,v in await cur.fetchall()}

    result_members = []
    unassigned_count = 0
    for member in members:
        member_periods = periods.get(member["id"], [])
        unavailable = [
            label
            for label, target_date in (
                ("saturday", saturday),
                ("sunday", sunday),
            )
            if period_covers(target_date, member_periods)
        ]
        exempt = len(unavailable) == 2
        assignment = assignments.get(member["id"], [])
        recorded = member["id"] in recorded_member_ids
        week_days = []
        for offset in range(7):
            target_date = week_start + timedelta(days=offset)
            status = resolve_day(
                member,
                target_date,
                daily,
                member_periods,
                duty_positions,
            )
            week_days.append(
                {
                    "date": target_date.isoformat(),
                    "state": status["state"],
                    "source": status["source"],
                    "scheduled": (member["id"], target_date) in daily,
                    "conflict": status["conflict"],
                }
            )
        complete = all(label in unavailable or (member["id"], day) in daily for label,day in (("saturday",saturday),("sunday",sunday)))
        if not complete:
            unassigned_count += 1
        result_members.append(
            {
                **member,
                "duty_days": assignment,
                "week_days": week_days,
                "attendance_revision": versions.get(member["id"], 0),
                "recorded": recorded,
                "complete": complete,
                "previous_duty_days": previous_assignments.get(member["id"], []),
                "previous_recorded": all((member["id"], d-timedelta(days=7)) in daily for d in (saturday,sunday)),
                "unavailable_days": unavailable,
                "exempt": exempt,
                "absence_reason": "",
                "conflict": any(day in unavailable for day in assignment),
            }
        )

    return {
        "model_version": 2,
        "week_start": week_start.isoformat(),
        "saturday": saturday.isoformat(),
        "sunday": sunday.isoformat(),
        "positions": duty_positions,
        "members": result_members,
        "complete": unassigned_count == 0,
        "unassigned_count": unassigned_count,
    }


async def save_weekend_board(conn, *, requested_date, raw_assignments, updated_by, expected_versions, request_id):
    saturday, sunday = weekend_dates(requested_date)
    async def apply(cur, versions, dry):
        board = await get_weekend_board(cur, requested_date)
        members = {m["id"]:m for m in board["members"]}
        if not set(raw_assignments).issubset(members):
            raise ValueError("包含不存在或不参与备勤的人员")
        daily = await load_daily(cur, list(raw_assignments), saturday, sunday)
        for m, days in raw_assignments.items():
            member = members[m]
            if set(days) - {"saturday","sunday"}:
                raise ValueError("备勤选项无效")
            changes = []
            for label, day in (("saturday",saturday),("sunday",sunday)):
                state = "work" if label in days else "rest"
                old = daily.get((m,day))
                # Existing work covered by a newly entered leave is preserved.
                if state == "work" and label in member["unavailable_days"] and old != "work":
                    raise ValueError("所选日期有有效请假，请先处理请假记录")
                if old != state:
                    changes.append(dict(date=day.isoformat(),before=old,after=state))
                    await cur.execute(f"INSERT INTO {table('_personnel_work_schedule')} "
                                      "(member_id,work_date,state,source,revision,updated_by) VALUES (%s,%s,%s,'weekend',%s,%s) "
                                      "ON DUPLICATE KEY UPDATE state=VALUES(state),source=VALUES(source),revision=VALUES(revision),updated_by=VALUES(updated_by),updated_at=UTC_TIMESTAMP()",
                                      (m,day,state,versions[m]+1,updated_by))
            if changes:
                await record_change(cur,m,versions[m],'weekend',updated_by,'双休日排班调整',[],changes)
        return await get_weekend_board(cur, requested_date)
    return await mutation(conn, actor=updated_by, request_id=request_id,
                          payload=dict(week=str(requested_date),assignments=raw_assignments,versions=expected_versions),
                          member_ids=list(raw_assignments),expected=expected_versions,callback=apply)


async def get_attendance_context(
    cur,
    *,
    start_date: date,
    end_date: date,
    selected_positions: set[str],
    community_scope: list[str] | None = None,
    member_names: list[str] | None = None,
) -> dict[str, Any]:
    if selected_positions:
        placeholders = ", ".join(["%s"] * len(selected_positions))
        scope_clause = ""
        scope_params: list[str] = []
        if community_scope is not None:
            if community_scope:
                scope_placeholders = ", ".join(["%s"] * len(community_scope))
                scope_clause = (
                    "AND EXISTS (SELECT 1 "
                    f"FROM {table('_grid_member_department_links')} AS scope_link "
                    f"JOIN {table('_departments')} AS scope_department "
                    "ON scope_department.id=scope_link.department_id "
                    f"JOIN {table('_communities')} AS scope_community "
                    "ON scope_community.id=scope_department.community_id "
                    "WHERE scope_link.member_id=member.id "
                    f"AND scope_community.name IN ({scope_placeholders}))"
                )
                scope_params = list(community_scope)
            else:
                scope_clause = "AND 1=0"
        member_clause = ""
        member_params: list[str] = []
        if member_names is not None:
            normalized_names = [
                str(name).strip() for name in member_names if str(name).strip()
            ]
            if normalized_names:
                member_placeholders = ", ".join(["%s"] * len(normalized_names))
                member_clause = f"AND member.name IN ({member_placeholders})"
                member_params = normalized_names
            else:
                member_clause = "AND 1=0"
        await cur.execute(
            f"""
            SELECT member.id, member.name, community.name, member.position,
                   link.sort_order
            FROM {table('_grid_members')} AS member
            LEFT JOIN {table('_grid_member_department_links')} AS link
              ON link.member_id=member.id
            LEFT JOIN {table('_departments')} AS department
              ON department.id=link.department_id
            LEFT JOIN {table('_communities')} AS community
              ON community.id=department.community_id
            WHERE member.position IN ({placeholders})
              {scope_clause}
              {member_clause}
            ORDER BY member.name, link.sort_order, community.name
            """,
            [*sorted(selected_positions), *scope_params, *member_params],
        )
        member_rows = await cur.fetchall()
    else:
        member_rows = []
    members: dict[str, dict[str, Any]] = {}
    for row in member_rows:
        name = str(row[1])
        member = members.setdefault(name, {
            "id": int(row[0]),
            "name": name,
            "community": "未分配社区",
            "communities": [],
            "position": str(row[3]),
        })
        community = str(row[2] or "").strip()
        if community and community not in member["communities"]:
            member["communities"].append(community)
            if member["community"] == "未分配社区":
                member["community"] = community
    periods = await list_attendance_periods(
        cur,
        start_date=start_date,
        end_date=end_date,
        member_ids=[member["id"] for member in members.values()],
    )

    configured_duty_positions = set(await get_configured_positions(
        cur,
        WEEKEND_DUTY_POSITION_CONFIG_KEY,
    ))
    duty_members = [
        member
        for member in members.values()
        if member["position"] in configured_duty_positions
    ]
    business_today = await get_business_date(cur)
    week_starts = required_week_starts(start_date, end_date, business_today)
    daily = await load_daily(cur, [member["id"] for member in members.values()], start_date, end_date)
    missing_weeks: set[date] = set()
    for target in iter_dates(start_date, min(end_date, business_today)):
        if target.weekday() < 5:
            continue
        for member in duty_members:
            status = resolve_day(member, target, daily, periods.get(member["id"], []), configured_duty_positions)
            if status["state"] == "unassigned":
                missing_weeks.add(normalize_week_start(target))

    await cur.execute(
        f"""
        SELECT config_value
        FROM {table('_system_config')}
        WHERE config_key='attendance_history_started_on'
        """
    )
    history_row = await cur.fetchone()
    try:
        history_started_on = (
            date.fromisoformat(str(history_row[0]))
            if history_row and history_row[0]
            else None
        )
    except ValueError:
        history_started_on = None
    legacy_history_incomplete = (
        history_started_on is None or start_date < history_started_on
    )
    return {
        "members": members,
        "periods": periods,
        "daily": daily,
        "weekend_duty_positions": configured_duty_positions,
        "missing_week_starts": missing_weeks,
        "history_started_on": history_started_on,
        "legacy_history_incomplete": legacy_history_incomplete,
    }


def is_member_on_duty(
    member: dict[str, Any],
    target_date: date,
    context: dict[str, Any],
) -> bool:
    return resolve_day(member, target_date, context.get("daily", {}),
                       context["periods"].get(member["id"], []),
                       context.get("weekend_duty_positions", DEFAULT_WEEKEND_DUTY_POSITIONS))["state"] == "work"


def allocate_person_days(
    *,
    start_date: date,
    end_date: date,
    daily_visits: dict[tuple[date, str], dict[str, int]],
    context: dict[str, Any],
    include_unknown: bool,
) -> dict[str, Any]:
    allocations: dict[str, Decimal] = defaultdict(Decimal)
    total_person_days = Decimal(0)
    worked_while_off = 0
    worked_while_leave = 0
    unknown_participant_days = 0
    members = context["members"]

    for target_date in iter_dates(start_date, end_date):
        for name, member in members.items():
            visits = daily_visits.get((target_date, name), {})
            status = resolve_day(
                member,
                target_date,
                context.get("daily", {}),
                context["periods"].get(member["id"], []),
                context.get("weekend_duty_positions", DEFAULT_WEEKEND_DUTY_POSITIONS),
            )
            on_duty = status["state"] == "work"
            if visits and not on_duty:
                if status["state"] in {"leave", "long_term_leave"}:
                    worked_while_leave += 1
                else:
                    worked_while_off += 1
            if not on_duty:
                continue
            total_person_days += Decimal(1)
            if visits:
                visit_total = sum(visits.values())
                for community, count in visits.items():
                    allocations[community] += (
                        Decimal(count) / Decimal(visit_total)
                    )
            else:
                communities = member.get("communities") or [member["community"]]
                for community in communities:
                    allocations[community] += Decimal(1)

    if include_unknown:
        for (target_date, name), visits in daily_visits.items():
            if name in members:
                continue
            unknown_participant_days += 1
            total_person_days += Decimal(1)
            visit_total = sum(visits.values())
            for community, count in visits.items():
                allocations[community] += (
                    Decimal(count) / Decimal(visit_total)
                )

    complete = not context["missing_week_starts"]
    return {
        "community_person_days": dict(allocations),
        "total_person_days": total_person_days,
        "complete": complete,
        "missing_week_starts": sorted(context["missing_week_starts"]),
        "history_started_on": context["history_started_on"],
        "legacy_history_incomplete": context["legacy_history_incomplete"],
        "worked_while_off": worked_while_off,
        "worked_while_leave": worked_while_leave,
        "unknown_participant_days": unknown_participant_days,
    }
