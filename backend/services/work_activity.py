"""Privacy-safe contribution events for public work profiles."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from config import settings
from database import db_manager
from services.business_time import resolve_timezone


logger = logging.getLogger(__name__)

ONLINE_TASK_UPDATE = "online_task_update"
POLICE_DISPATCH_REVIEW = "police_dispatch_review"
WORK_LOG = "work_log"

ACTIVITY_LABELS = {
    ONLINE_TASK_UPDATE: "指令核查处理",
    POLICE_DISPATCH_REVIEW: "下发任务审核",
    WORK_LOG: "工作日志编制",
}

# Assignment-only fields such as inspector/community deliberately stay out.
ACTUAL_ONLINE_WORK_FIELDS = frozenset({
    "现住址",
    "核查结果",
    "核查反馈",
    "实际情况",
    "二次反馈",
    "二次核查结果",
    "二次反馈/二次核查结果",
    "研判",
    "登记情况",
})


def actual_online_work_fields(columns: Iterable[str]) -> list[str]:
    """Return only fields that represent completed investigation work."""
    return sorted({str(column) for column in columns} & ACTUAL_ONLINE_WORK_FIELDS)


def is_actual_online_work(columns: Iterable[str]) -> bool:
    return bool(actual_online_work_fields(columns))


def work_profile_key(user_id: int, member_id: int | None) -> str:
    """Keep personnel contribution stable when their linked account changes."""
    return f"member:{member_id}" if member_id is not None else f"user:{user_id}"


def profile_identity(user: dict) -> tuple[str, int, int | None]:
    user_id = int(user["id"])
    raw_member_id = (user.get("member") or {}).get("id")
    member_id = int(raw_member_id) if raw_member_id is not None else None
    return work_profile_key(user_id, member_id), user_id, member_id


async def record_work_activity(
    user: dict,
    activity_type: str,
    *,
    event_key: str,
    units: int = 1,
    conn=None,
) -> bool:
    """Insert one idempotent event; contribution failure never breaks the work."""
    if activity_type not in ACTIVITY_LABELS:
        logger.warning("Ignored unknown work activity type: %s", activity_type)
        return False
    if units < 1:
        return False
    try:
        profile_key, user_id, member_id = profile_identity(user)
        pool = None
        owns_connection = conn is None
        if owns_connection:
            pool = db_manager.get_pool("online_data")
            conn = await asyncio.wait_for(
                pool.acquire(),
                timeout=settings.MYSQL_POOL_ACQUIRE_TIMEOUT_SECONDS,
            )
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT IGNORE INTO _work_activity_events (
                        event_key, profile_key, user_id, member_id,
                        activity_type, units, occurred_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s,
                              UTC_TIMESTAMP(), UTC_TIMESTAMP())
                    """,
                    (
                        event_key[:190],
                        profile_key,
                        user_id,
                        member_id,
                        activity_type,
                        int(units),
                    ),
                )
                return cur.rowcount == 1
        finally:
            if owns_connection and pool is not None:
                pool.release(conn)
    except Exception as exc:  # contribution is secondary to the completed work
        logger.warning(
            "Failed to record privacy-safe work activity type=%s: %s",
            activity_type,
            type(exc).__name__,
        )
        return False


def contribution_summary(
    rows: Iterable[tuple[str, int, datetime]],
    *,
    timezone_name: str,
) -> dict:
    """Aggregate UTC events into business-local days and public summaries."""
    timezone_info = resolve_timezone(timezone_name)
    daily: Counter[date] = Counter()
    categories: Counter[str] = Counter()
    for activity_type, units, occurred_at in rows:
        amount = max(0, int(units or 0))
        if amount == 0:
            continue
        timestamp = occurred_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        business_day = timestamp.astimezone(timezone_info).date()
        daily[business_day] += amount
        categories[str(activity_type)] += amount

    active_dates = sorted(daily)
    longest_streak = 0
    current_streak = 0
    previous: date | None = None
    for active_date in active_dates:
        current_streak = (
            current_streak + 1
            if previous and active_date == previous + timedelta(days=1)
            else 1
        )
        longest_streak = max(longest_streak, current_streak)
        previous = active_date

    return {
        "total": sum(daily.values()),
        "active_days": len(active_dates),
        "longest_streak": longest_streak,
        "days": [
            {"date": item.isoformat(), "count": int(daily[item])}
            for item in active_dates
        ],
        "categories": [
            {
                "type": activity_type,
                "label": ACTIVITY_LABELS.get(activity_type, activity_type),
                "count": int(count),
            }
            for activity_type, count in sorted(
                categories.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
    }
