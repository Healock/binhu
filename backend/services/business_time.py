"""系统业务日期和 UTC 时间范围。

数据库中的 DATETIME 按 UTC 保存；日报日期按系统设置中的时区判断。
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"


def resolve_timezone(timezone_name: str | None) -> ZoneInfo:
    """读取有效时区；配置错误时安全回退到上海时区。"""
    try:
        return ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def current_business_date(
    timezone_name: str | None,
    now: datetime | None = None,
) -> date:
    """按指定时区返回当前业务日期。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(resolve_timezone(timezone_name)).date()


def business_date_range_utc_bounds(
    start_date: str,
    end_date: str,
    timezone_name: str | None,
) -> tuple[datetime, datetime]:
    """把包含首尾两天的业务日期范围换算成 UTC 左闭右开范围。"""
    timezone_info = resolve_timezone(timezone_name)
    start = datetime.combine(
        date.fromisoformat(start_date),
        time.min,
        tzinfo=timezone_info,
    )
    end = datetime.combine(
        date.fromisoformat(end_date) + timedelta(days=1),
        time.min,
        tzinfo=timezone_info,
    )
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


async def get_business_timezone_name(cur) -> str:
    """从系统设置读取有效时区名称。"""
    await cur.execute(
        "SELECT config_value FROM OnlineData._system_config "
        "WHERE config_key = 'timezone'"
    )
    row = await cur.fetchone()
    timezone_name = row[0] if row and row[0] else DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
        return timezone_name
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return DEFAULT_TIMEZONE


async def get_business_date(cur) -> date:
    """按系统设置中的时区返回当前业务日期。"""
    timezone_name = await get_business_timezone_name(cur)
    return current_business_date(timezone_name)


async def get_business_date_from_db() -> date:
    """从数据库读取系统时区并返回当前业务日期。"""
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            return await get_business_date(cur)
    finally:
        pool.release(conn)


async def get_business_date_range_utc_bounds(
    cur,
    start_date: str,
    end_date: str,
) -> tuple[datetime, datetime]:
    """读取系统时区并把业务日期范围换算成 UTC。"""
    timezone_name = await get_business_timezone_name(cur)
    return business_date_range_utc_bounds(start_date, end_date, timezone_name)
