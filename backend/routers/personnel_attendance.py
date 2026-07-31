"""人员出勤历史和双休日备勤 API。"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from database import get_db
from deps import require_permission
from services.permissions import (
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
from services.personnel_positions import (
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    get_configured_positions,
)

router = APIRouter(
    prefix="/api/personnel/attendance",
    tags=["人员出勤"],
)


class WeekendAssignment(BaseModel):
    member_id: int = Field(gt=0)
    duty_day: Literal["saturday", "sunday"] | None = None


class WeekendDutyUpdate(BaseModel):
    week_start: date
    assignments: list[WeekendAssignment] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_unique_members(self):
        member_ids = [item.member_id for item in self.assignments]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("同一人员不能重复排班")
        return self


@router.get("/weekend-duty")
async def read_weekend_duty(
    week_start: date = Query(...),
    user: dict = Depends(require_permission(PERSONNEL_BASIC_VIEW)),
    conn=Depends(get_db),
):
    del user
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
        "missing_week_starts": [
            week_start.isoformat()
            for week_start in missing_weeks
        ],
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
                item.member_id: item.duty_day
                for item in data.assignments
            },
            updated_by=int(user["id"]),
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
        **request_audit_fields(request),
    )
    return board


@router.get("/history")
async def read_attendance_history(
    member_id: int | None = Query(default=None, gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_permission(PERSONNEL_SENSITIVE_VIEW)),
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
            "SELECT COUNT(*) FROM _personnel_attendance_history AS history"
            + where,
            params,
        )
        total = int((await cur.fetchone())[0])
        await cur.execute(
            """
            SELECT history.id, history.member_id, member.name,
                   history.absence_type, history.start_date,
                   history.end_date, history.reason, history.source,
                   history.is_active, history.created_at
            FROM _personnel_attendance_history AS history
            LEFT JOIN _grid_members AS member
              ON member.id=history.member_id
            """
            + where
            + " ORDER BY history.start_date DESC, history.id DESC "
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
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
