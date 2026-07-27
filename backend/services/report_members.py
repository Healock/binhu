"""用网格员名册补全核查人明细中的零工作行。"""

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from services.grid_member_status import active_member_sql


ZERO_METRICS = (0, 0, 0, 0, 0, 0, 0)


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _is_zero_metric(value: Any) -> bool:
    try:
        return Decimal(str(value or 0)) == 0
    except (InvalidOperation, ValueError):
        return False


def _is_persisted_zero_row(row: Sequence[Any]) -> bool:
    return len(row) >= 9 and all(_is_zero_metric(value) for value in row[2:9])


async def get_active_members(cur, as_of_date: str) -> list[tuple[str, str]]:
    """读取指定日期实际在岗的网格员，返回（社区，姓名）。"""
    active_condition = active_member_sql("g")
    await cur.execute(
        f"""
        SELECT
            COALESCE(NULLIF(TRIM(g.community), ''), '未分配社区'),
            TRIM(g.name)
        FROM OnlineData._grid_members g
        WHERE TRIM(g.name) <> ''
          AND {active_condition}
        ORDER BY g.community, g.name
        """,
        (as_of_date,),
    )
    return [
        (str(community or "未分配社区"), str(name).strip())
        for community, name in await cur.fetchall()
    ]


def get_missing_zero_rows(
    existing_rows: Iterable[Sequence[Any]],
    active_members: Iterable[tuple[str, str]],
) -> list[tuple[Any, ...]]:
    """找出没有任何统计行的在岗人员，并为他们生成全零行。"""
    existing_names = {
        _normalized_name(row[1])
        for row in existing_rows
        if len(row) > 1 and _normalized_name(row[1])
    }
    missing_rows = []
    for community, name in active_members:
        normalized = _normalized_name(name)
        if not normalized or normalized in existing_names:
            continue
        missing_rows.append((community, name, *ZERO_METRICS))
        existing_names.add(normalized)
    return missing_rows


async def complete_inspector_rows(
    cur,
    existing_rows: Iterable[Sequence[Any]],
    as_of_date: str,
) -> list[tuple[Any, ...]]:
    """查询日报时补全旧报表，不修改历史日报表。"""
    rows = [
        tuple(row)
        for row in existing_rows
        if not _is_persisted_zero_row(row)
    ]
    active_members = await get_active_members(cur, as_of_date)
    rows.extend(get_missing_zero_rows(rows, active_members))
    rows.sort(key=lambda row: (str(row[0] or ""), str(row[1] or "")))
    return rows


async def insert_zero_member_rows(cur, inspector_table: str, as_of_date: str) -> int:
    """生成新日报时，把零工作人员一并写入核查人日报表。"""
    await cur.execute(f"SELECT 社区, 姓名 FROM {inspector_table}")
    existing_rows = await cur.fetchall()
    active_members = await get_active_members(cur, as_of_date)
    missing_rows = get_missing_zero_rows(existing_rows, active_members)
    if missing_rows:
        await cur.executemany(
            f"""
            INSERT INTO {inspector_table}
                (社区, 姓名, 数据总数, 未核查, 已核查, 已完成,
                 核查完成率, 无法见底数, 核查见底率)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            missing_rows,
        )
    return len(missing_rows)
