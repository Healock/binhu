"""Role-aware dashboard assembled from existing business sources."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends

from database import get_db
from deps import get_current_user
from routers.mobile_tasks import _aggregate_live
from routers.police_dispatch import ALLOWED_POLICE_POSITIONS, _batch_payloads
from services.business_time import (
    business_date_range_utc_bounds,
    get_business_date,
    get_business_timezone_name,
)
from services.dashboard_scope import (
    dashboard_communities,
    is_admin_account,
    is_super_admin_account,
    member_position,
    responsibility_label,
)
from services.data_scope import community_names_for_scopes
from services.permissions import (
    ONLINE_RAW_VIEW,
    ONLINE_SUMMARY_VIEW,
    OPS_MANAGE,
    POLICE_DISPATCH_MANAGE,
    VISIT_SUMMARY_VIEW,
    has_permission,
)
from services.report_overview import SUMMARY_TYPE, get_online_overview
from services.report_range import get_summary_range
from services.task_workflow import MOBILE_TASK_TYPES, TASK_WORKFLOWS
from services.visit_summary import (
    VISIT_CATEGORY_RENTAL,
    get_visit_summary,
)
from services.work_activity import contribution_summary, profile_identity


router = APIRouter(prefix="/api/dashboard", tags=["岗位仪表盘"])


def _iso_utc(value: Any) -> str | None:
    if not value:
        return None
    return value.isoformat() + "Z" if isinstance(value, datetime) else str(value)


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


async def _load_common(cur, user: dict, business_date: date) -> dict:
    await cur.execute(
        """
        SELECT id, status, trigger_source, phase, started_at, finished_at
        FROM _sync_log ORDER BY id DESC LIMIT 1
        """
    )
    sync_row = await cur.fetchone()
    await cur.execute(
        "SELECT MAX(finished_at) FROM _sync_log "
        "WHERE status IN ('success', 'completed')"
    )
    last_success = await cur.fetchone()
    await cur.execute(
        "SELECT COUNT(*) FROM _notifications WHERE user_id=%s AND is_read=0",
        (user["id"],),
    )
    personal_unread = _count((await cur.fetchone())[0])
    await cur.execute(
        """
        SELECT COUNT(*)
        FROM _announcements AS announcement
        LEFT JOIN _announcement_reads AS reading
          ON reading.announcement_id=announcement.id AND reading.user_id=%s
        WHERE announcement.is_active=1
          AND announcement.published_at <= UTC_TIMESTAMP()
          AND (announcement.expires_at IS NULL
               OR announcement.expires_at > UTC_TIMESTAMP())
          AND reading.announcement_id IS NULL
        """,
        (user["id"],),
    )
    announcement_unread = _count((await cur.fetchone())[0])
    departments = [
        str(item.get("name") or "").strip()
        for item in user.get("departments") or []
        if str(item.get("name") or "").strip()
    ]
    return {
        "business_date": business_date.isoformat(),
        "last_success_at": _iso_utc(last_success[0] if last_success else None),
        "sync": {
            "id": int(sync_row[0]) if sync_row else None,
            "status": str(sync_row[1] or "") if sync_row else "idle",
            "trigger_source": str(sync_row[2] or "") if sync_row else "",
            "stage": str(sync_row[3] or "") if sync_row else "",
            "created_at": _iso_utc(sync_row[4]) if sync_row else None,
            "finished_at": _iso_utc(sync_row[5]) if sync_row else None,
        },
        "notifications": {
            "unread_count": personal_unread + announcement_unread,
            "personal_unread_count": personal_unread,
            "announcement_unread_count": announcement_unread,
        },
        "identity": {
            "user_id": int(user["id"]),
            "display_name": str(
                (user.get("member") or {}).get("name")
                or user.get("display_name")
                or "平台用户"
            ),
            "position": member_position(user) or "平台账号",
            "departments": departments,
        },
    }


async def _load_contribution(
    cur,
    user: dict,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> dict:
    start, end = business_date_range_utc_bounds(
        start_date.isoformat(), end_date.isoformat(), timezone_name
    )
    profile_key, _, _ = profile_identity(user)
    await cur.execute(
        """
        SELECT activity_type, units, occurred_at
        FROM _work_activity_events
        WHERE profile_key=%s AND occurred_at >= %s AND occurred_at < %s
        ORDER BY occurred_at
        """,
        (profile_key, start, end),
    )
    summary = contribution_summary(
        await cur.fetchall(), timezone_name=timezone_name
    )
    counts = {
        str(item.get("date")): _count(item.get("count"))
        for item in summary.get("days") or []
    }
    summary["days"] = [
        {
            "date": (start_date + timedelta(days=offset)).isoformat(),
            "count": counts.get(
                (start_date + timedelta(days=offset)).isoformat(), 0
            ),
        }
        for offset in range((end_date - start_date).days + 1)
    ]
    summary["start_date"] = start_date.isoformat()
    summary["end_date"] = end_date.isoformat()
    summary["profile_user_id"] = int(user["id"])
    return summary


async def _load_flow_tasks(
    conn,
    cur,
    user: dict,
    business_date: date,
    start_date: date,
) -> dict | None:
    position = member_position(user)
    if position not in {"组员", "组长"} or not has_permission(user, ONLINE_RAW_VIEW):
        return None
    member = user.get("member") or {}
    name = str(member.get("name") or "").strip()
    communities = [
        str(value).strip()
        for value in user.get("community_names") or []
        if str(value).strip()
    ]
    if not name or len(communities) != 1:
        return {
            "available": False,
            "message": "当前人员需要配置一个有效社区部门",
            "businesses": [],
        }
    formal_community = communities[0]
    aliases = await community_names_for_scopes(conn, [formal_community])
    context = {
        "name": name,
        "position": position,
        "community": formal_community,
        "community_values": aliases or [formal_community],
        "admin_mode": False,
    }
    personal = await _aggregate_live(cur, context, "mine")
    community = await _aggregate_live(cur, context, "community")
    today = await get_online_overview(
        business_date.isoformat(),
        business_date.isoformat(),
        SUMMARY_TYPE,
        [formal_community],
        inspector=name,
        parser_types_override=list(MOBILE_TASK_TYPES),
    )
    trend_inspector = name if position == "组员" else None
    week = await get_online_overview(
        start_date.isoformat(),
        business_date.isoformat(),
        SUMMARY_TYPE,
        [formal_community],
        inspector=trend_inspector,
        parser_types_override=list(MOBILE_TASK_TYPES),
    )
    personal_items = list(personal.values())
    community_items = list(community.values())
    businesses = sorted(
        personal_items,
        key=lambda item: (item["pending"] == 0, -item["pending"], item["label"]),
    )
    return {
        "available": True,
        "scope": "mine",
        "community": formal_community,
        "personal": {
            "pending": sum(_count(item["pending"]) for item in personal_items),
            "review": sum(_count(item["review"]) for item in personal_items),
            "new_today": _count(today.get("new_tasks")) if today.get("exists") else None,
            "carryover_today": _count(today.get("carryover_tasks")) if today.get("exists") else None,
            "completed_today": _count(today.get("completed_tasks")) if today.get("exists") else None,
        },
        "community_totals": {
            "pending": sum(_count(item["pending"]) for item in community_items),
            "review": sum(_count(item["review"]) for item in community_items),
        },
        "daily_snapshot_available": bool(today.get("exists")),
        "businesses": businesses,
        "week_overview": week,
    }


def _community_breakdown(report: dict, communities: list[str] | None) -> list[dict]:
    rows = list((report.get("community") or {}).get("data") or [])
    if communities is not None:
        accepted = set(communities)
        rows = [row for row in rows if str(row.get("社区") or "").strip() in accepted]
    result = []
    for row in rows:
        total = _count(row.get("数据总数"))
        completed = _count(row.get("已完成"))
        result.append({
            "community": str(row.get("社区") or ""),
            "total": total,
            "pending": max(total - completed, 0),
            "completed": completed,
            "unable_to_verify": _count(row.get("无法见底数")),
            "completion_rate": _ratio(completed, total),
        })
    return sorted(result, key=lambda item: (-item["pending"], item["community"]))


async def _load_online_overview(
    cur,
    user: dict,
    business_date: date,
    start_date: date,
) -> dict | None:
    if (
        not has_permission(user, ONLINE_SUMMARY_VIEW)
        or member_position(user) == "自购房"
    ):
        return None
    communities = await dashboard_communities(cur, user, ONLINE_SUMMARY_VIEW)
    inspector = None
    if member_position(user) == "组员":
        inspector = str((user.get("member") or {}).get("name") or "").strip() or None
    today = await get_online_overview(
        business_date.isoformat(), business_date.isoformat(), SUMMARY_TYPE,
        communities, inspector=inspector,
    )
    week = await get_online_overview(
        start_date.isoformat(), business_date.isoformat(), SUMMARY_TYPE,
        communities, inspector=inspector,
    )
    report = await get_summary_range(start_date.isoformat(), business_date.isoformat())
    breakdown = [] if inspector else _community_breakdown(report, communities)
    if inspector:
        accepted_communities = set(communities or [])
        unable = sum(
            _count(row.get("无法见底数"))
            for row in (report.get("inspector") or {}).get("data") or []
            if str(row.get("姓名") or "").strip().casefold()
            == inspector.casefold()
            and (
                communities is None
                or str(row.get("社区") or "").strip() in accepted_communities
            )
        )
    else:
        unable = sum(item["unable_to_verify"] for item in breakdown)
    return {
        "scope": "responsibility",
        "scope_label": responsibility_label(member_position(user), communities),
        "communities": communities,
        "today": today,
        "week": {**week, "unable_to_verify": unable},
        "community_breakdown": breakdown,
    }


async def _personal_visit_metrics(
    cur,
    name: str,
    start_date: date,
    end_date: date,
) -> dict:
    await cur.execute(
        """
        SELECT COUNT(*),
               COALESCE(SUM(`新增` + `变更` + `注销`), 0),
               COUNT(`星级采集时间`),
               COUNT(DISTINCT NULLIF(TRIM(`社区`), ''))
        FROM t_visit_details
        WHERE `业务日期` BETWEEN %s AND %s
          AND LOWER(TRIM(`操作人`))=LOWER(TRIM(%s))
        """,
        (start_date, end_date, name),
    )
    row = await cur.fetchone()
    visits = _count(row[0] if row else 0)
    ratings = _count(row[2] if row else 0)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "visits": visits,
        "total_changes": _count(row[1] if row else 0),
        "rated_records": ratings,
        "unrated_records": max(visits - ratings, 0),
        "community_count": _count(row[3] if row else 0),
    }


async def _load_visit_overview(
    conn,
    cur,
    user: dict,
    business_date: date,
    start_date: date,
) -> dict | None:
    if not has_permission(user, VISIT_SUMMARY_VIEW):
        return None
    position = member_position(user)
    if position in {"组员", "组长"}:
        return None
    if position == "自购房":
        name = str((user.get("member") or {}).get("name") or "").strip()
        return {
            "category": "self_owned",
            "scope": "mine",
            "today": await _personal_visit_metrics(cur, name, business_date, business_date),
            "week": await _personal_visit_metrics(cur, name, start_date, business_date),
            "community_breakdown": [],
        }
    communities = await dashboard_communities(cur, user, VISIT_SUMMARY_VIEW)
    aliases = (
        await community_names_for_scopes(conn, communities)
        if communities is not None
        else None
    )
    today = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_RENTAL,
        community_scope=communities,
        community_names=aliases,
    )
    week = await get_visit_summary(
        conn,
        start_date,
        business_date,
        category=VISIT_CATEGORY_RENTAL,
        community_scope=communities,
        community_names=aliases,
    )
    return {
        "category": "rental",
        "scope": "responsibility",
        "scope_label": responsibility_label(position, communities),
        "today": today.get("overview") or {},
        "week": week.get("overview") or {},
        "attendance": week.get("attendance") or {},
        "community_breakdown": list((week.get("community") or {}).get("data") or []),
    }


def _can_view_dispatch(user: dict) -> bool:
    if not has_permission(user, POLICE_DISPATCH_MANAGE):
        return False
    scope = (user.get("permission_scopes") or {}).get(
        POLICE_DISPATCH_MANAGE, user.get("data_scope")
    )
    if scope != "all":
        return False
    member = user.get("member")
    return (
        member_position(user) in ALLOWED_POLICE_POSITIONS
        if member
        else is_admin_account(user)
    )


async def _load_dispatch_overview(cur, user: dict) -> dict | None:
    if not _can_view_dispatch(user):
        return None
    await cur.execute(
        """
        SELECT id FROM _police_dispatch_batches
        WHERE status<>'completed' ORDER BY created_at, id LIMIT 1
        """
    )
    active = await cur.fetchone()
    if not active:
        return {"active_batch": None}
    payloads = await _batch_payloads(cur, [int(active[0])])
    return {"active_batch": payloads[0] if payloads else None}


async def _load_management(cur, user: dict, dispatch: dict | None, sync: dict) -> dict | None:
    if not is_admin_account(user):
        return None
    await cur.execute(
        "SELECT config_value FROM _system_config "
        "WHERE config_key='online_writeback_enabled'"
    )
    writeback = await cur.fetchone()
    result: dict[str, Any] = {
        "sync": sync,
        "online_writeback_enabled": str(writeback[0] if writeback else "0").lower()
        in {"1", "true", "yes", "on"},
        "dispatch_exceptions": 0,
    }
    active = (dispatch or {}).get("active_batch") or {}
    counts = active.get("counts") or {}
    result["dispatch_exceptions"] = sum(
        _count(counts.get(key))
        for key in ("needs_reconciliation", "conflict", "retryable")
    )
    if is_super_admin_account(user) and has_permission(user, OPS_MANAGE):
        await cur.execute(
            """
            SELECT status, created_at, finished_at
            FROM _backup_jobs ORDER BY id DESC LIMIT 1
            """
        )
        backup = await cur.fetchone()
        result["latest_backup"] = {
            "status": str(backup[0] or "") if backup else "none",
            "created_at": _iso_utc(backup[1]) if backup else None,
            "finished_at": _iso_utc(backup[2]) if backup else None,
        }
    return result


@router.get("")
async def get_dashboard(
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        timezone_name = await get_business_timezone_name(cur)
        business_date = await get_business_date(cur)
        start_date = business_date - timedelta(days=6)
        common = await _load_common(cur, user, business_date)
        responsibility = await dashboard_communities(
            cur,
            user,
            ONLINE_SUMMARY_VIEW if has_permission(user, ONLINE_SUMMARY_VIEW) else ONLINE_RAW_VIEW,
        )
        common["scope"] = {
            "kind": "responsibility",
            "label": responsibility_label(member_position(user), responsibility),
            "communities": responsibility,
        }
        contribution = await _load_contribution(
            cur, user, start_date, business_date, timezone_name
        )
        flow_tasks = await _load_flow_tasks(conn, cur, user, business_date, start_date)
        online_overview = await _load_online_overview(cur, user, business_date, start_date)
        visit_overview = await _load_visit_overview(conn, cur, user, business_date, start_date)
        dispatch_overview = await _load_dispatch_overview(cur, user)
        management = await _load_management(
            cur, user, dispatch_overview, common["sync"]
        )

    return {
        **common,
        "period": {
            "start_date": start_date.isoformat(),
            "end_date": business_date.isoformat(),
            "days": 7,
        },
        "contribution": contribution,
        "flow_tasks": flow_tasks,
        "online_overview": online_overview,
        "visit_overview": visit_overview,
        "dispatch_overview": dispatch_overview,
        "management": management,
    }
