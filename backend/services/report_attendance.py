"""在线总汇总的逐日在岗人日计算。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from database import db_manager
from services.personnel_attendance import (
    get_attendance_context,
    is_member_on_duty,
    normalize_week_start,
)
from services.report_members import canonical_community


SUMMARY_POSITIONS = {"组长", "组员"}


async def get_community_person_days(
    cur,
    covered_dates: set[str],
    alias_lookup: dict[str, str],
) -> tuple[dict[str, int], dict]:
    """按实际有快照的日期累计社区在岗人日。"""
    dates = sorted(date.fromisoformat(value) for value in covered_dates)
    context = await get_attendance_context(
        cur,
        start_date=dates[0],
        end_date=dates[-1],
        selected_positions=SUMMARY_POSITIONS,
    )
    covered_weekends = {
        normalize_week_start(target_date)
        for target_date in dates
        if target_date.weekday() >= 5
    }
    missing_weeks = sorted(
        week_start
        for week_start in context["missing_week_starts"]
        if week_start in covered_weekends
    )

    person_days: dict[str, int] = defaultdict(int)
    for target_date in dates:
        for member in context["members"].values():
            if not is_member_on_duty(member, target_date, context):
                continue
            communities = {
                canonical_community(community, alias_lookup)
                for community in (
                    member.get("communities") or [member.get("community")]
                )
                if str(community or "").strip()
            }
            for community in communities:
                person_days[community] += 1

    return dict(person_days), {
        "complete": not missing_weeks,
        "missing_week_starts": [value.isoformat() for value in missing_weeks],
        "history_started_on": (
            context["history_started_on"].isoformat()
            if context.get("history_started_on")
            else None
        ),
        "legacy_history_incomplete": bool(
            context.get("legacy_history_incomplete")
        ),
    }


async def load_community_person_days(
    covered_dates: set[str],
    alias_lookup: dict[str, str],
) -> tuple[dict[str, int], dict]:
    """从 OnlineData 读取出勤，避免复用 daily_report 的数据库连接。"""
    if not covered_dates:
        raise ValueError("计算在岗人日时至少需要一个业务日期")
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            return await get_community_person_days(
                cur,
                covered_dates,
                alias_lookup,
            )
    finally:
        pool.release(conn)
