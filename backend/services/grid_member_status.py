"""网格员长期状态、请假区间与实际状态的统一规则。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


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
            "status_detail": "长期离岗",
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


async def get_business_date(cur) -> date:
    """按系统配置的时区取得今天，配置异常时使用上海时区。"""
    await cur.execute(
        "SELECT config_value FROM _system_config WHERE config_key = 'timezone'"
    )
    row = await cur.fetchone()
    timezone_name = row[0] if row and row[0] else "Asia/Shanghai"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Asia/Shanghai")
    return datetime.now(timezone).date()
