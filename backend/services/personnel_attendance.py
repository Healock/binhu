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
        """
        SELECT member_id, absence_type, start_date, end_date,
               reason, source, is_active
        FROM _personnel_attendance_history
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
        SELECT id, name, community, position
        FROM _grid_members
        WHERE position IN ({placeholders})
        ORDER BY community, position, name
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
        start_date=saturday,
        end_date=sunday,
        member_ids=member_ids,
    )

    await cur.execute(
        """
        SELECT member_id, duty_date
        FROM _personnel_weekend_duty
        WHERE week_start=%s
        """,
        (week_start,),
    )
    assignments = {int(row[0]): row[1] for row in await cur.fetchall()}
    recorded_member_ids = set(assignments)

    previous_week = week_start - timedelta(days=7)
    previous_saturday, previous_sunday = weekend_dates(previous_week)
    await cur.execute(
        """
        SELECT member_id, duty_date
        FROM _personnel_weekend_duty
        WHERE week_start=%s
        """,
        (previous_week,),
    )
    previous_assignments = {
        int(row[0]): duty_label(row[1], previous_saturday, previous_sunday)
        for row in await cur.fetchall()
    }

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
        assignment = duty_label(
            assignments.get(member["id"]),
            saturday,
            sunday,
        )
        if assignment in unavailable:
            assignment = None
        recorded = member["id"] in recorded_member_ids
        if not exempt and not recorded:
            unassigned_count += 1
        reasons = sorted({
            str(period.get("reason") or "请假")
            for period in member_periods
            if any(
                period_covers(target_date, [period])
                for target_date in (saturday, sunday)
            )
        })
        result_members.append(
            {
                **member,
                "assignment": assignment,
                "recorded": recorded,
                "previous_assignment": previous_assignments.get(member["id"]),
                "unavailable_days": unavailable,
                "exempt": exempt,
                "absence_reason": "；".join(reasons),
            }
        )

    return {
        "week_start": week_start.isoformat(),
        "saturday": saturday.isoformat(),
        "sunday": sunday.isoformat(),
        "positions": duty_positions,
        "members": result_members,
        "complete": unassigned_count == 0,
        "unassigned_count": unassigned_count,
    }


async def save_weekend_board(
    conn,
    *,
    requested_date: date,
    raw_assignments: dict[int, str | None],
    updated_by: int,
) -> dict[str, Any]:
    week_start = normalize_week_start(requested_date)
    saturday, sunday = weekend_dates(week_start)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            board = await get_weekend_board(cur, week_start)
            members = {member["id"]: member for member in board["members"]}
            unknown_ids = set(raw_assignments) - set(members)
            missing_ids = set(members) - set(raw_assignments)
            if unknown_ids:
                raise ValueError("排班中包含不存在或不参与排班的人员")
            if missing_ids:
                raise ValueError("请提交本周全部备勤人员的安排")

            rows: list[tuple[Any, ...]] = []
            for member_id, member in members.items():
                assignment = raw_assignments[member_id]
                unavailable = set(member["unavailable_days"])
                if member["exempt"]:
                    if assignment is not None:
                        raise ValueError(f"{member['name']}周末两天都请假，无需排班")
                    duty_date = None
                else:
                    if assignment not in {None, "saturday", "sunday"}:
                        raise ValueError(f"{member['name']}的备勤选项无效")
                    if assignment is not None and assignment in unavailable:
                        raise ValueError(f"{member['name']}所选日期处于请假状态")
                    duty_date = (
                        saturday
                        if assignment == "saturday"
                        else sunday if assignment == "sunday" else None
                    )
                rows.append(
                    (
                        week_start,
                        member_id,
                        duty_date,
                        member["name"],
                        member["community"],
                        member["position"],
                        updated_by,
                    )
                )

            if rows:
                await cur.executemany(
                    """
                    INSERT INTO _personnel_weekend_duty (
                        week_start, member_id, duty_date, member_name,
                        community_snapshot, position_snapshot, updated_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        duty_date=VALUES(duty_date),
                        member_name=VALUES(member_name),
                        community_snapshot=VALUES(community_snapshot),
                        position_snapshot=VALUES(position_snapshot),
                        updated_by=VALUES(updated_by)
                    """,
                    rows,
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    async with conn.cursor() as cur:
        return await get_weekend_board(cur, week_start)


async def get_attendance_context(
    cur,
    *,
    start_date: date,
    end_date: date,
    selected_positions: set[str],
) -> dict[str, Any]:
    if selected_positions:
        placeholders = ", ".join(["%s"] * len(selected_positions))
        await cur.execute(
            f"""
            SELECT id, name, community, position
            FROM _grid_members
            WHERE position IN ({placeholders})
            ORDER BY community, name
            """,
            sorted(selected_positions),
        )
        member_rows = await cur.fetchall()
    else:
        member_rows = []
    members = {
        str(row[1]): {
            "id": int(row[0]),
            "name": str(row[1]),
            "community": str(row[2] or "未分配社区"),
            "position": str(row[3]),
        }
        for row in member_rows
    }
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
    week_starts = sorted({
        normalize_week_start(target_date)
        for target_date in iter_dates(start_date, end_date)
        if target_date.weekday() >= 5
    })
    duties: dict[tuple[int, date], date | None] = {}
    if week_starts and duty_members:
        placeholders = ", ".join(["%s"] * len(week_starts))
        await cur.execute(
            f"""
            SELECT week_start, member_id, duty_date
            FROM _personnel_weekend_duty
            WHERE week_start IN ({placeholders})
            """,
            week_starts,
        )
        for row in await cur.fetchall():
            duties[(int(row[1]), row[0])] = row[2]

    missing_weeks: set[date] = set()
    for week_start in week_starts:
        saturday, sunday = weekend_dates(week_start)
        for member in duty_members:
            member_periods = periods.get(member["id"], [])
            unavailable_both = (
                period_covers(saturday, member_periods)
                and period_covers(sunday, member_periods)
            )
            if (
                not unavailable_both
                and (member["id"], week_start) not in duties
            ):
                missing_weeks.add(week_start)

    await cur.execute(
        """
        SELECT config_value
        FROM _system_config
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
        "duties": duties,
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
    if period_covers(
        target_date,
        context["periods"].get(member["id"], []),
    ):
        return False
    if target_date.weekday() < 5:
        return True
    duty_positions = context.get(
        "weekend_duty_positions",
        DEFAULT_WEEKEND_DUTY_POSITIONS,
    )
    if member["position"] not in duty_positions:
        return False
    week_start = normalize_week_start(target_date)
    return context["duties"].get((member["id"], week_start)) == target_date


def allocate_person_days(
    *,
    start_date: date,
    end_date: date,
    daily_visits: dict[tuple[date, str], dict[str, int]],
    context: dict[str, Any],
    include_unknown: bool,
) -> dict[str, Any]:
    allocations: dict[str, Decimal] = defaultdict(Decimal)
    worked_while_off = 0
    unknown_participant_days = 0
    members = context["members"]

    for target_date in iter_dates(start_date, end_date):
        for name, member in members.items():
            visits = daily_visits.get((target_date, name), {})
            on_duty = is_member_on_duty(member, target_date, context)
            if visits and not on_duty:
                on_duty = True
                worked_while_off += 1
            if not on_duty:
                continue
            if visits:
                visit_total = sum(visits.values())
                for community, count in visits.items():
                    allocations[community] += (
                        Decimal(count) / Decimal(visit_total)
                    )
            else:
                allocations[member["community"]] += Decimal(1)

    if include_unknown:
        for (target_date, name), visits in daily_visits.items():
            if name in members:
                continue
            unknown_participant_days += 1
            visit_total = sum(visits.values())
            for community, count in visits.items():
                allocations[community] += (
                    Decimal(count) / Decimal(visit_total)
                )

    complete = not context["missing_week_starts"]
    return {
        "community_person_days": dict(allocations),
        "total_person_days": sum(allocations.values(), Decimal(0)),
        "complete": complete,
        "missing_week_starts": sorted(context["missing_week_starts"]),
        "history_started_on": context["history_started_on"],
        "legacy_history_incomplete": context["legacy_history_incomplete"],
        "worked_while_off": worked_while_off,
        "unknown_participant_days": unknown_participant_days,
    }
