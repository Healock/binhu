"""人员临时请假、长期和实际状态的统一规则。"""

from datetime import date

from services.business_time import get_business_date


def validate_leave_period(start_date: date | None, end_date: date | None) -> None:
    """请假日期必须同时填写，且结束日期不能早于开始日期。"""
    if (start_date is None) != (end_date is None):
        raise ValueError("请假开始和结束日期需要同时填写")
    if start_date and end_date and end_date < start_date:
        raise ValueError("请假结束日期不能早于开始日期")


def get_status_snapshot(
    base_status: str,
    leave_start_date: date | None,
    leave_end_date: date | None,
    as_of: date,
) -> dict[str, str | None]:
    """返回指定日期的实际状态和请假阶段。"""
    if base_status == "离岗":
        return {
            "effective_status": "离岗",
            "status_detail": "长期",
            "leave_state": None,
        }

    if leave_start_date and leave_end_date:
        if leave_start_date <= as_of <= leave_end_date:
            return {
                "effective_status": "离岗",
                "status_detail": f"请假至 {leave_end_date.isoformat()}",
                "leave_state": "active",
            }
        if as_of < leave_start_date:
            return {
                "effective_status": "在岗",
                "status_detail": f"{leave_start_date.isoformat()} 起请假",
                "leave_state": "upcoming",
            }
        return {
            "effective_status": "在岗",
            "status_detail": "",
            "leave_state": "expired",
        }

    return {
        "effective_status": "在岗",
        "status_detail": "",
        "leave_state": None,
    }


def apply_weekend_duty_status(
    snapshot: dict[str, str | None],
    *,
    position: str,
    as_of: date,
    duty_positions: set[str],
    duty_recorded: bool,
    duty_date: date | None,
) -> dict[str, str | None]:
    """Project today's recorded weekend roster into the personnel status."""
    if (
        as_of.weekday() < 5
        or position not in duty_positions
        or snapshot.get("effective_status") == "离岗"
    ):
        return snapshot

    result = dict(snapshot)
    if not duty_recorded:
        result.update({
            "effective_status": "未排班",
            "status_detail": "本周双休日备勤尚未安排",
        })
    elif duty_date == as_of:
        result.update({
            "effective_status": "在岗",
            "status_detail": "今日备勤",
        })
    else:
        if duty_date is None:
            detail = "本周双休日休息"
        else:
            duty_day = "周六" if duty_date.weekday() == 5 else "周日"
            detail = f"{duty_day}备勤，今日休息"
        result.update({
            "effective_status": "休息",
            "status_detail": detail,
        })
    return result


def active_member_sql(alias: str = "", date_placeholder: str = "%s") -> str:
    """生成“指定日期实际在岗”的 SQL 条件。"""
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}status = '在岗' "
        f"AND NOT ("
        f"{prefix}leave_start_date IS NOT NULL "
        f"AND {prefix}leave_end_date IS NOT NULL "
        f"AND {date_placeholder} BETWEEN {prefix}leave_start_date AND {prefix}leave_end_date"
        f")"
    )
