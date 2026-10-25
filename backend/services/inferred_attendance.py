"""Read-only attendance suggestions based on formal schedules and stable visit identity.

The result is deliberately a proposal.  It never writes a schedule and never
turns a name-only online ledger row into personal attendance evidence.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import re
from typing import Any

from services.attendance_schedule import load_daily, load_periods, resolve_day
from services.personnel_positions import (
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    get_configured_positions,
    personnel_table,
)

MAX_SUGGESTION_DAYS = 31
STABLE_ACCOUNT_PATTERN = re.compile(r"^(?:\d{15}|\d{17}[0-9X])$")


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def normalize_account(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _member_entry(row: tuple[Any, ...]) -> dict[str, Any]:
    member_id, name, position, account, community = row
    return {
        "id": int(member_id),
        "name": str(name or "").strip(),
        "position": str(position or "").strip(),
        "account": normalize_account(account),
        "communities": {str(community or "").strip()} - {""},
    }


def _status_label(state: str) -> str:
    return {
        "work": "work",
        "rest": "rest",
        "unassigned": "unassigned",
        "leave": "leave",
        "long_term_leave": "long_term_leave",
    }.get(state, state)


def build_suggestions(
    *,
    start_date: date,
    end_date: date,
    members: list[dict[str, Any]],
    daily: dict[tuple[int, date], str],
    periods: dict[int, list[dict[str, Any]]],
    duty_positions: set[str],
    evidence: dict[tuple[date, int], dict[str, Any]],
    online_unusable_by_date: dict[date, int] | None = None,
) -> dict[str, Any]:
    """Build a safe proposal from already loaded rows.

    ``evidence`` is keyed by stable member id.  Callers must not construct it
    from a name-only source.
    """
    online_unusable_by_date = online_unusable_by_date or {}
    rows: list[dict[str, Any]] = []
    official_person_days = 0
    suggested_person_days = 0
    conflict_count = 0
    needs_confirmation_count = 0
    evidence_days = 0

    for target in _days(start_date, end_date):
        for member in members:
            status = resolve_day(
                member,
                target,
                daily,
                periods.get(member["id"], []),
                duty_positions,
            )
            official = status["state"]
            item_evidence = evidence.get((target, member["id"]), {})
            raw_visit_count = int(item_evidence.get("visit_count", 0) or 0)
            communities = sorted(
                str(value).strip()
                for value in item_evidence.get("communities", [])
                if str(value).strip()
            )
            visit_count = raw_visit_count if communities else 0
            # A visit without a formal community cannot safely support a
            # person-level attendance suggestion.
            has_evidence = visit_count > 0
            if has_evidence:
                evidence_days += 1

            if official == "work":
                official_person_days += 1
                inferred = "work"
                suggestion_state = "official_work"
            elif official in {"leave", "long_term_leave"}:
                inferred = official
                suggestion_state = "leave_conflict" if has_evidence else "official_leave"
                if has_evidence:
                    conflict_count += 1
            elif has_evidence:
                inferred = "work"
                suggestion_state = "suggested_work"
                suggested_person_days += 1
            elif official == "unassigned":
                inferred = None
                suggestion_state = "needs_confirmation"
                needs_confirmation_count += 1
            else:
                inferred = official
                suggestion_state = "official_rest"

            rows.append(
                {
                    "date": target.isoformat(),
                    "member_id": member["id"],
                    "member_name": member["name"],
                    "communities": sorted(member.get("communities") or []),
                    "official_status": _status_label(official),
                    "inferred_status": inferred,
                    "suggestion_state": suggestion_state,
                    "evidence_summary": {
                        "visit_count": visit_count,
                        "communities": communities,
                        "online_rows_not_personalized": int(
                            online_unusable_by_date.get(target, 0)
                        ),
                    },
                }
            )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "source": "formal_schedule_plus_stable_visit_identity",
        "read_only": True,
        "rows": rows,
        "summary": {
            "member_days": len(rows),
            "official_person_days": official_person_days,
            "suggested_person_days": suggested_person_days,
            "evidence_days": evidence_days,
            "leave_conflicts": conflict_count,
            "needs_confirmation": needs_confirmation_count,
            "online_rows_not_personalized": sum(online_unusable_by_date.values()),
        },
        "online_evidence_note": (
            "在线指令流水目前没有稳定人员编号，仅作为整体来源提示，未用于个人建议。"
        ),
    }


async def load_suggestions(cur, *, start_date: date, end_date: date) -> dict[str, Any]:
    if start_date > end_date:
        raise ValueError("起始日期不能晚于结束日期")
    if (end_date - start_date).days + 1 > MAX_SUGGESTION_DAYS:
        raise ValueError(f"系统建议查询区间不能超过 {MAX_SUGGESTION_DAYS} 天")

    duty_positions = set(
        await get_configured_positions(cur, WEEKEND_DUTY_POSITION_CONFIG_KEY)
    )
    marks = ",".join(["%s"] * len(duty_positions))
    await cur.execute(
        f"""
        SELECT member.id, member.name, member.position, member.id_card_number,
               community.name
        FROM {personnel_table('_grid_members')} AS member
        LEFT JOIN {personnel_table('_grid_member_department_links')} AS link
          ON link.member_id=member.id
        LEFT JOIN {personnel_table('_departments')} AS department
          ON department.id=link.department_id
        LEFT JOIN {personnel_table('_communities')} AS community
          ON community.id=department.community_id
        WHERE member.position IN ({marks}) AND TRIM(member.name) <> ''
        ORDER BY member.name, member.id, community.name
        """,
        sorted(duty_positions),
    )
    members_by_id: dict[int, dict[str, Any]] = {}
    for row in await cur.fetchall():
        member = members_by_id.setdefault(int(row[0]), _member_entry(row))
        community = str(row[4] or "").strip()
        if community:
            member["communities"].add(community)

    members = list(members_by_id.values())
    member_ids = [member["id"] for member in members]
    daily = await load_daily(cur, member_ids, start_date, end_date)
    periods = await load_periods(cur, member_ids, start_date, end_date)
    account_to_member: dict[str, dict[str, Any]] = {}
    ambiguous_accounts: set[str] = set()
    for member in members:
        account = member["account"]
        if not account or not STABLE_ACCOUNT_PATTERN.fullmatch(account):
            continue
        if account in account_to_member:
            ambiguous_accounts.add(account)
        else:
            account_to_member[account] = member
    for account in ambiguous_accounts:
        account_to_member.pop(account, None)

    evidence: dict[tuple[date, int], dict[str, Any]] = {}
    await cur.execute(
        """
        SELECT `业务日期`, `社区`, `操作人账号`, COUNT(DISTINCT `_row_key`)
        FROM t_visit_details
        WHERE `业务日期` BETWEEN %s AND %s
          AND TRIM(`操作人账号`) <> ''
        GROUP BY `业务日期`, `社区`, `操作人账号`
        """,
        (start_date, end_date),
    )
    for business_date, community, account, count in await cur.fetchall():
        member = account_to_member.get(normalize_account(account))
        if not member:
            continue
        community_name = str(community or "").strip()
        if not community_name or community_name not in member["communities"]:
            continue
        key = (business_date, member["id"])
        bucket = evidence.setdefault(
            key, {"visit_count": 0, "communities": set()}
        )
        bucket["visit_count"] += int(count or 0)
        if community_name:
            bucket["communities"].add(community_name)

    # Name-only online rows are intentionally not used for person matching.
    online_unusable_by_date: dict[date, int] = defaultdict(int)
    from config import settings

    if settings.DAILY_DOMAIN_ACTIVE:
        await cur.execute(
            """
            SELECT report_date, COUNT(*)
            FROM _daily_task_ledger
            WHERE report_date BETWEEN %s AND %s
              AND included=1
              AND TRIM(COALESCE(inspector, '')) <> ''
            GROUP BY report_date
            """,
            (start_date, end_date),
        )
        online_unusable_by_date.update(
            (business_date, int(count or 0))
            for business_date, count in await cur.fetchall()
        )

    for item in evidence.values():
        item["communities"] = sorted(item["communities"])
    return build_suggestions(
        start_date=start_date,
        end_date=end_date,
        members=members,
        daily=daily,
        periods=periods,
        duty_positions=duty_positions,
        evidence=evidence,
        online_unusable_by_date=dict(online_unusable_by_date),
    )
