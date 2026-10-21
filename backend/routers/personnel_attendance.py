"""人员出勤历史和双休日备勤 API。"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from database import get_db
from deps import require_permission, get_current_user
from services.permissions import (
    has_permission,
    ATTENDANCE_MANAGE,
    PERSONNEL_BASIC_VIEW,
    PERSONNEL_SENSITIVE_VIEW,
)
from services.audit import record_admin_audit, request_audit_fields
from services.personnel_attendance import (
    get_attendance_context,
    get_weekend_board,
    normalize_week_start,
    save_weekend_board,
)
from services.attendance_schedule import (
    table,
    load_daily,
    load_periods,
    resolve_day,
    schedule_mutation,
    leave_mutation,
    ready,
)
from services.business_time import get_business_date
from services.personnel_positions import (
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    get_configured_positions,
)

router = APIRouter(
    prefix="/api/personnel/attendance",
    tags=["人员出勤"],
)


async def attendance_view(user: dict = Depends(get_current_user)):
    if not (
        has_permission(user, PERSONNEL_SENSITIVE_VIEW)
        or has_permission(user, ATTENDANCE_MANAGE)
    ):
        raise HTTPException(403, "完整出勤记录需要敏感资料查看或出勤管理权限")
    return user


class WeekendAssignment(BaseModel):
    member_id: int = Field(gt=0)
    duty_days: list[Literal["saturday", "sunday"]] = Field(max_length=2)
    expected_version: int = Field(ge=0)

    @model_validator(mode="after")
    def distinct_days(self):
        if len(set(self.duty_days)) != len(self.duty_days):
            raise ValueError("备勤日期重复")
        return self

    model_config = {"extra": "forbid"}


class WeekendDutyUpdate(BaseModel):
    week_start: date
    request_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    assignments: list[WeekendAssignment] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_unique_members(self):
        member_ids = [item.member_id for item in self.assignments]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("同一人员不能重复排班")
        return self


@router.get("/weekend-duty")
async def read_weekend_duty(
    week_start: date = Query(...),
    model_version: int = Query(default=0),
    user: dict = Depends(require_permission(PERSONNEL_BASIC_VIEW)),
    conn=Depends(get_db),
):
    del user
    if model_version != 2:
        raise HTTPException(
            409, "排班模型已升级，请刷新或升级客户端后查看，旧单日模型不再提供"
        )
    async with conn.cursor() as cur:
        return await get_weekend_board(cur, week_start)


@router.get("/status")
async def read_attendance_status(
    start_date: date = Query(...),
    end_date: date = Query(...),
    user: dict = Depends(require_permission(PERSONNEL_BASIC_VIEW)),
    conn=Depends(get_db),
):
    """检查所选区间内是否还有双休日未排班。"""
    del user
    if start_date > end_date:
        raise HTTPException(400, "起始日期不能晚于结束日期")
    if (end_date - start_date).days > 366:
        raise HTTPException(400, "排班检查区间不能超过 366 天")
    async with conn.cursor() as cur:
        duty_positions = await get_configured_positions(
            cur,
            WEEKEND_DUTY_POSITION_CONFIG_KEY,
        )
        context = await get_attendance_context(
            cur,
            start_date=start_date,
            end_date=end_date,
            selected_positions=set(duty_positions),
        )
    missing_weeks = sorted(context["missing_week_starts"])
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "complete": not missing_weeks,
        "missing_week_starts": [week_start.isoformat() for week_start in missing_weeks],
    }


@router.put("/weekend-duty")
async def update_weekend_duty(
    data: WeekendDutyUpdate,
    request: Request,
    user: dict = Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    normalized_week = normalize_week_start(data.week_start)
    try:
        board = await save_weekend_board(
            conn,
            requested_date=normalized_week,
            raw_assignments={
                item.member_id: item.duty_days for item in data.assignments
            },
            updated_by=int(user["id"]),
            expected_versions={
                item.member_id: item.expected_version for item in data.assignments
            },
            request_id=data.request_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_admin_audit(
        user,
        "personnel.weekend_duty.update",
        target_type="weekend_duty",
        target_name=normalized_week.isoformat(),
        detail={
            "member_count": len(data.assignments),
            "complete": board["complete"],
        },
        conn=conn,
        **request_audit_fields(request),
    )
    return board


@router.get("/history")
async def read_attendance_history(
    member_id: int | None = Query(default=None, gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(attendance_view),
    conn=Depends(get_db),
):
    del user
    where = ""
    params: list[object] = []
    if member_id is not None:
        where = " WHERE history.member_id=%s"
        params.append(member_id)
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _personnel_attendance_history AS history" + where,
            params,
        )
        total = int((await cur.fetchone())[0])
        await cur.execute(
            """
            SELECT history.id, history.member_id, member.name,
                   history.absence_type, history.start_date,
                   history.end_date, history.reason, history.source,
                   history.is_active, history.created_at, history.superseded_by,
                   history.correction_action, history.corrected_by, history.corrected_at,
                   history.workflow_ticket_id, member.attendance_revision, history.correction_reason
            FROM _personnel_attendance_history AS history
            LEFT JOIN _grid_members AS member
              ON member.id=history.member_id
            """ + where + " ORDER BY history.start_date DESC, history.id DESC "
            "LIMIT %s OFFSET %s",
            [*params, page_size, offset],
        )
        rows = await cur.fetchall()
    return {
        "data": [
            {
                "id": row[0],
                "member_id": row[1],
                "member_name": row[2] or "已删除人员",
                "absence_type": row[3],
                "start_date": row[4],
                "end_date": row[5],
                "reason": row[6] or "",
                "source": row[7],
                "is_active": bool(row[8]),
                "created_at": row[9],
                "superseded_by": row[10],
                "state": (
                    "active"
                    if row[8]
                    else (
                        "corrected"
                        if row[10]
                        else "revoked" if row[11] == "revoke" else "inactive"
                    )
                ),
                "corrected_by": row[12],
                "corrected_at": row[13],
                "workflow_ticket_id": row[14],
                "attendance_revision": int(row[15] or 0),
                "correction_reason": row[16],
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


class ScheduleChange(BaseModel):
    request_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    member_ids: list[int] = Field(min_length=1, max_length=100)
    expected_versions: dict[int, int]
    start_date: date
    end_date: date
    state: Literal["work", "rest"]
    original_start: date | None = None
    original_end: date | None = None
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_members(self):
        if len(set(self.member_ids)) != len(self.member_ids) or any(
            m <= 0 for m in self.member_ids
        ):
            raise ValueError("人员编号重复或无效")
        if set(self.expected_versions) != set(self.member_ids) or any(
            v < 0 for v in self.expected_versions.values()
        ):
            raise ValueError("每位人员必须提供版本号")
        if not self.reason.strip():
            raise ValueError("请填写调整原因")
        if (self.original_start is None) != (self.original_end is None):
            raise ValueError("原日期范围必须完整")
        return self


class LeaveChange(BaseModel):
    request_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    member_id: int = Field(gt=0)
    expected_version: int = Field(ge=0)
    record_id: int | None = Field(default=None, gt=0)
    action: Literal["create", "correct", "revoke", "resume"]
    absence_type: Literal["temporary_leave", "long_term_leave"] = "temporary_leave"
    start_date: date | None = None
    end_date: date | None = None
    leave_reason: str = Field(default="", max_length=200)
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_record(self):
        if (self.action == "create") != (self.record_id is None):
            raise ValueError("新建不能提供旧记录；更正、撤销及恢复必须提供旧记录")
        if not self.reason.strip():
            raise ValueError("请填写更正原因")
        return self


@router.post("/schedule/preview")
async def preview_schedule(
    data: ScheduleChange,
    user=Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    return await schedule_mutation(
        conn, data.model_dump(), int(user["id"]), preview=True
    )


@router.post("/schedule")
async def commit_schedule(
    data: ScheduleChange,
    user=Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    return await schedule_mutation(conn, data.model_dump(), int(user["id"]))


@router.post("/leave/preview")
async def preview_leave(
    data: LeaveChange,
    user=Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    return await leave_mutation(conn, data.model_dump(), int(user["id"]), preview=True)


@router.post("/leave")
async def commit_leave(
    data: LeaveChange,
    user=Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    return await leave_mutation(conn, data.model_dump(), int(user["id"]))


@router.get("/calendar-options")
async def calendar_options(
    keyword: str = Query(default="", max_length=100),
    department_id: int | None = Query(default=None, gt=0),
    user=Depends(attendance_view),
    conn=Depends(get_db),
):
    # Do not require personnel.basic.view in addition to attendance access.
    # This endpoint deliberately excludes phone, identity, notes and reasons.
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT id,name,parent_id FROM {table('_departments')} WHERE is_active=1 ORDER BY name,id"
        )
        departments = [
            dict(id=int(i), name=n, parent_id=p) for i, n, p in await cur.fetchall()
        ]
        selected = {department_id} if department_id else {d["id"] for d in departments}
        if department_id:
            while True:
                expanded = selected | {
                    d["id"] for d in departments if d["parent_id"] in selected
                }
                if expanded == selected:
                    break
                selected = expanded
        where, params = [], []
        if keyword.strip():
            where.append("m.name LIKE %s")
            params.append("%" + keyword.strip() + "%")
        if department_id:
            marks = ",".join(["%s"] * len(selected))
            where.append(
                f"EXISTS (SELECT 1 FROM {table('_grid_member_department_links')} l WHERE l.member_id=m.id AND l.department_id IN ({marks}))"
            )
            params.extend(sorted(selected))
        clause = " WHERE " + " AND ".join(where) if where else ""
        await cur.execute(
            f"SELECT m.id,m.name,m.position,m.attendance_revision FROM {table('_grid_members')} m{clause} ORDER BY m.name,m.id LIMIT 200",
            params,
        )
        members = [
            dict(id=int(i), name=n, position=p, attendance_revision=int(v))
            for i, n, p, v in await cur.fetchall()
        ]
    return dict(members=members, departments=departments)


@router.get("/calendar")
async def read_calendar(
    start_date: date,
    end_date: date,
    member_ids: list[int] = Query(default=[]),
    user=Depends(attendance_view),
    conn=Depends(get_db),
):
    if start_date > end_date or (end_date - start_date).days > 62:
        raise HTTPException(400, "日历区间不能超过 63 天")
    if (
        len(member_ids) > 100
        or len(set(member_ids)) != len(member_ids)
        or any(m <= 0 for m in member_ids)
    ):
        raise HTTPException(400, "请选择最多 100 位不重复的人员")
    async with conn.cursor() as cur:
        enabled = await ready(cur)
        today = await get_business_date(cur)
        await cur.execute(
            f"SELECT config_value FROM {table('_system_config')} WHERE config_key='attendance_history_started_on'"
        )
        history_row = await cur.fetchone()
        history_started = (
            str(history_row[0]) if history_row and history_row[0] else None
        )
        incomplete = not history_started or start_date.isoformat() < history_started
        if not member_ids:
            return dict(members=[], days=[], editable=enabled, business_today=today)
        marks = ",".join(["%s"] * len(member_ids))
        await cur.execute(
            f"SELECT id,name,position,attendance_revision FROM {table('_grid_members')} WHERE id IN ({marks}) ORDER BY name,id",
            member_ids,
        )
        members = [
            dict(id=int(m), name=n, position=p, attendance_revision=int(v))
            for m, n, p, v in await cur.fetchall()
        ]
        if len(members) != len(member_ids):
            raise HTTPException(404, "部分人员已不存在，请重新选择")
        daily = await load_daily(cur, member_ids, start_date, end_date)
        periods = await load_periods(cur, member_ids, start_date, end_date)
        positions = set(
            await get_configured_positions(cur, WEEKEND_DUTY_POSITION_CONFIG_KEY)
        )
    from services.attendance_schedule import dates

    days = [
        dict(
            member_id=m["id"],
            date=d,
            **resolve_day(m, d, daily, periods.get(m["id"], []), positions),
        )
        for m in members
        for d in dates(start_date, end_date)
    ]
    return dict(
        members=members,
        days=days,
        editable=enabled,
        business_today=today,
        legacy_history_incomplete=incomplete,
        leaves=[dict(member_id=m, **p) for m, ps in periods.items() for p in ps],
    )
