"""Public, privacy-safe people directory and work contribution profiles."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db
from services.business_time import (
    business_date_range_utc_bounds,
    current_business_date,
    get_business_timezone_name,
)
from services.work_activity import contribution_summary, work_profile_key


router = APIRouter(prefix="/api/profiles", tags=["人员主页"])


PROFILE_SELECT = """
    SELECT user.id,
           COALESCE(
               NULLIF(member.name, ''),
               NULLIF(user.display_name, ''),
               '未命名用户'
           ) AS display_name,
           COALESCE(member.position, '') AS position,
           COALESCE(
               departments.department_names,
               primary_department.name,
               ''
           ) AS department_names,
           COALESCE(
               departments.community_names,
               primary_community.name,
               ''
           ) AS community_names,
           user.created_at AS joined_at,
           user.member_id AS member_id
    FROM _users AS user
    LEFT JOIN _grid_members AS member ON member.id=user.member_id
    LEFT JOIN _departments AS primary_department
      ON primary_department.id=member.department_id
    LEFT JOIN _communities AS primary_community
      ON primary_community.id=primary_department.community_id
    LEFT JOIN (
        SELECT link.member_id,
               GROUP_CONCAT(
                   DISTINCT department.name
                   ORDER BY department.name SEPARATOR '、'
               ) AS department_names,
               GROUP_CONCAT(
                   DISTINCT CASE
                       WHEN department.department_type='community'
                       THEN COALESCE(community.name, department.name)
                       ELSE NULL
                   END
                   ORDER BY community.name SEPARATOR '、'
               ) AS community_names
        FROM _grid_member_department_links AS link
        JOIN _departments AS department ON department.id=link.department_id
        LEFT JOIN _communities AS community
          ON community.id=department.community_id
        GROUP BY link.member_id
    ) AS departments ON departments.member_id=member.id
"""


def _split_names(value: object) -> list[str]:
    return [item for item in str(value or "").split("、") if item]


def _profile_payload(row) -> dict:
    user_id = int(row[0])
    member_id = int(row[6]) if row[6] is not None else None
    joined_at = row[5]
    return {
        "id": user_id,
        "profile_key": work_profile_key(user_id, member_id),
        "display_name": str(row[1]),
        "position": str(row[2] or ""),
        "departments": _split_names(row[3]),
        "community_names": _split_names(row[4]),
        "joined_at": joined_at.isoformat() + "Z" if joined_at else None,
    }


def _year_bounds(year: int, timezone_name: str):
    return business_date_range_utc_bounds(
        date(year, 1, 1).isoformat(),
        date(year, 12, 31).isoformat(),
        timezone_name,
    )


async def _load_event_summaries(
    cur,
    profile_keys: list[str],
    *,
    year: int,
    timezone_name: str,
) -> dict[str, dict]:
    if not profile_keys:
        return {}
    start, end = _year_bounds(year, timezone_name)
    placeholders = ",".join(["%s"] * len(profile_keys))
    await cur.execute(
        f"""
        SELECT profile_key, activity_type, units, occurred_at
        FROM _work_activity_events
        WHERE profile_key IN ({placeholders})
          AND occurred_at >= %s AND occurred_at < %s
        ORDER BY occurred_at
        """,
        (*profile_keys, start, end),
    )
    grouped: dict[str, list[tuple[str, int, object]]] = defaultdict(list)
    for profile_key, activity_type, units, occurred_at in await cur.fetchall():
        grouped[str(profile_key)].append(
            (str(activity_type), int(units or 0), occurred_at)
        )
    return {
        profile_key: contribution_summary(rows, timezone_name=timezone_name)
        for profile_key, rows in grouped.items()
    }


def _empty_summary() -> dict:
    return {
        "total": 0,
        "active_days": 0,
        "longest_streak": 0,
        "days": [],
        "categories": [],
    }


@router.get("")
async def list_profiles(
    keyword: str = Query(default="", max_length=100),
    position: str = Query(default="", max_length=30),
    year: int | None = Query(default=None, ge=2000, le=2100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    conn=Depends(get_db),
):
    """Every logged-in account can browse non-sensitive public profiles."""
    search = keyword.strip()
    position_filter = position.strip()
    like = f"%{search}%"
    where = """
        WHERE (%s='' OR
               COALESCE(NULLIF(member.name, ''), NULLIF(user.display_name, ''), '') LIKE %s OR
               COALESCE(member.position, '') LIKE %s OR
               COALESCE(departments.department_names, primary_department.name, '') LIKE %s)
          AND (%s='' OR COALESCE(member.position, '')=%s)
    """
    params = (search, like, like, like, position_filter, position_filter)
    async with conn.cursor() as cur:
        timezone_name = await get_business_timezone_name(cur)
        current_year = current_business_date(timezone_name).year
        selected_year = year or current_year
        if selected_year > current_year:
            raise HTTPException(400, "不能查看未来年份的工作贡献")
        await cur.execute(
            "SELECT COUNT(*) FROM (" + PROFILE_SELECT + where + ") AS profiles",
            params,
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            PROFILE_SELECT
            + where
            + " ORDER BY display_name, user.id LIMIT %s OFFSET %s",
            (*params, page_size, (page - 1) * page_size),
        )
        profiles = [_profile_payload(row) for row in await cur.fetchall()]
        summaries = await _load_event_summaries(
            cur,
            [profile["profile_key"] for profile in profiles],
            year=selected_year,
            timezone_name=timezone_name,
        )

    for profile in profiles:
        profile["contribution"] = summaries.get(
            profile.pop("profile_key"),
            _empty_summary(),
        )
    return {
        "data": profiles,
        "total": total,
        "page": page,
        "page_size": page_size,
        "year": selected_year,
    }


@router.get("/{user_id}")
async def get_profile(
    user_id: int,
    year: int | None = Query(default=None, ge=2000, le=2100),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        timezone_name = await get_business_timezone_name(cur)
        current_year = current_business_date(timezone_name).year
        selected_year = year or current_year
        if selected_year > current_year:
            raise HTTPException(400, "不能查看未来年份的工作贡献")
        await cur.execute(PROFILE_SELECT + " WHERE user.id=%s", (user_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "人员主页不存在")
        profile = _profile_payload(row)
        summaries = await _load_event_summaries(
            cur,
            [profile["profile_key"]],
            year=selected_year,
            timezone_name=timezone_name,
        )

    joined_year = row[5].year if row[5] else current_year
    available_start = min(max(2000, joined_year), current_year)
    profile["contribution"] = summaries.get(
        profile.pop("profile_key"),
        _empty_summary(),
    )
    profile["year"] = selected_year
    profile["available_years"] = list(
        range(current_year, available_start - 1, -1)
    )
    return profile
