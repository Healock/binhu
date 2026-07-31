"""用网格员名册补全核查人明细中的零工作行。"""

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
import unicodedata

from services.grid_member_status import active_member_sql
from services.personnel_positions import (
    ONLINE_POSITION_CONFIG_KEY,
    filter_person_rows,
    get_configured_positions,
    get_known_personnel_positions,
)


ZERO_METRICS = (0, 0, 0, 0, 0, 0, 0)


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _normalized_community(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


async def get_community_alias_lookup(cur) -> dict[str, str]:
    """读取社区正式名称和别名，返回“来源名称 -> 正式名称”映射。"""
    await cur.execute(
        """
        SELECT community.name, alias.alias
        FROM OnlineData._communities AS community
        LEFT JOIN OnlineData._community_aliases AS alias
          ON alias.community_id = community.id
        """
    )
    lookup: dict[str, str] = {}
    for formal_name, alias in await cur.fetchall():
        formal = _normalized_community(formal_name)
        if not formal:
            continue
        lookup[formal] = formal
        normalized_alias = _normalized_community(alias)
        if normalized_alias:
            lookup[normalized_alias] = formal
    return lookup


def canonical_community(
    value: Any,
    alias_lookup: dict[str, str],
) -> str:
    """按当前别名配置返回正式社区名，未匹配时保留来源名称。"""
    normalized = _normalized_community(value)
    return alias_lookup.get(normalized, normalized or "未分配社区")


def canonicalize_inspector_rows(
    rows: Iterable[Sequence[Any]],
    alias_lookup: dict[str, str],
) -> list[tuple[Any, ...]]:
    """合并“正式社区 + 别名社区”下的同一人员统计行。"""
    totals: dict[tuple[str, str], list[int]] = {}
    names: dict[tuple[str, str], str] = {}
    for row in rows:
        if len(row) < 9:
            continue
        community = canonical_community(row[0], alias_lookup)
        name = str(row[1] or "").strip()
        key = (community, _normalized_name(name))
        if not key[1]:
            continue
        bucket = totals.setdefault(key, [0, 0, 0, 0, 0])
        bucket[0] += int(row[2] or 0)
        bucket[1] += int(row[3] or 0)
        bucket[2] += int(row[4] or 0)
        bucket[3] += int(row[5] or 0)
        bucket[4] += int(row[7] or 0)
        names.setdefault(key, name)

    result = []
    for (community, normalized_name), counts in totals.items():
        total, unchecked, checked, completed, unable = counts
        result.append(
            (
                community,
                names[(community, normalized_name)],
                total,
                unchecked,
                checked,
                completed,
                calculate_ratio(completed, total),
                unable,
                calculate_ratio(max(completed - unable, 0), completed),
            )
        )
    result.sort(key=lambda row: (str(row[0] or ""), str(row[1] or "")))
    return result


def canonicalize_community_rows(
    rows: Iterable[Sequence[Any]],
    alias_lookup: dict[str, str],
) -> list[tuple[Any, ...]]:
    """按别名把社区统计行归并到正式社区，并重新计算比例。"""
    normalized_rows = [
        (canonical_community(row[0], alias_lookup), *row[1:8])
        for row in rows
        if len(row) >= 8
    ]
    return merge_community_rows(normalized_rows)


def _is_zero_metric(value: Any) -> bool:
    try:
        return Decimal(str(value or 0)) == 0
    except (InvalidOperation, ValueError):
        return False


def _is_persisted_zero_row(row: Sequence[Any]) -> bool:
    return len(row) >= 9 and all(_is_zero_metric(value) for value in row[2:9])


async def get_active_members(
    cur,
    as_of_date: str,
    positions: list[str] | None = None,
) -> list[tuple[str, str]]:
    """读取指定日期实际在岗的网格员，返回（社区，姓名）。"""
    positions = positions or await get_configured_positions(
        cur,
        ONLINE_POSITION_CONFIG_KEY,
    )
    placeholders = ", ".join(["%s"] * len(positions))
    active_condition = active_member_sql("g")
    await cur.execute(
        f"""
        SELECT
            COALESCE(NULLIF(TRIM(community.name), ''), '未分配社区'),
            TRIM(g.name)
        FROM OnlineData._grid_members g
        LEFT JOIN OnlineData._departments AS department
          ON department.id=g.department_id
        LEFT JOIN OnlineData._communities AS community
          ON community.id=department.community_id
        WHERE TRIM(g.name) <> ''
          AND g.position IN ({placeholders})
          AND {active_condition}
        ORDER BY community.name, g.name
        """,
        (*positions, as_of_date),
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
    selected_positions = await get_configured_positions(
        cur,
        ONLINE_POSITION_CONFIG_KEY,
    )
    known_positions = await get_known_personnel_positions(cur)
    scoped_rows = filter_person_rows(
        existing_rows,
        name_index=1,
        selected_positions=set(selected_positions),
        known_positions=known_positions,
    )
    rows = [
        tuple(row)
        for row in scoped_rows
        if not _is_persisted_zero_row(row)
    ]
    active_members = await get_active_members(
        cur,
        as_of_date,
        selected_positions,
    )
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


def calculate_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(
        (Decimal(numerator) / Decimal(denominator)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def aggregate_community_rows(
    inspector_rows: Iterable[Sequence[Any]],
) -> list[tuple[Any, ...]]:
    """由人员行重建社区行，保证岗位筛选后两张表口径一致。"""
    totals: dict[str, list[int]] = {}
    for row in inspector_rows:
        if len(row) < 9:
            continue
        community = str(row[0] or "未分配社区")
        bucket = totals.setdefault(community, [0, 0, 0, 0, 0])
        bucket[0] += int(row[2] or 0)
        bucket[1] += int(row[3] or 0)
        bucket[2] += int(row[4] or 0)
        bucket[3] += int(row[5] or 0)
        bucket[4] += int(row[7] or 0)

    result = []
    for community in sorted(totals):
        total, unchecked, checked, completed, unable = totals[community]
        result.append(
            (
                community,
                total,
                unchecked,
                checked,
                completed,
                calculate_ratio(completed, total),
                unable,
                calculate_ratio(max(completed - unable, 0), completed),
            )
        )
    return result


def merge_community_rows(
    community_rows: Iterable[Sequence[Any]],
) -> list[tuple[Any, ...]]:
    """合并多张分汇总表的社区行，并重新计算比例。"""
    synthetic_inspector_rows = [
        (
            row[0],
            "",
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        )
        for row in community_rows
        if len(row) >= 8
    ]
    return aggregate_community_rows(synthetic_inspector_rows)


def merge_inspector_rows(
    inspector_rows: Iterable[Sequence[Any]],
    canonical_members: Iterable[tuple[str, str]] = (),
) -> list[tuple[Any, ...]]:
    """合并多张分汇总表的人员行，并按当前名册统一社区和姓名。

    同一个姓名在多个业务分表中只保留一行。数量字段相加，比例根据
    合并后的数量重新计算；名册外但确有业务数据的人员仍然保留。
    """
    canonical = {
        _normalized_name(name): (
            str(community or "未分配社区"),
            str(name).strip(),
        )
        for community, name in canonical_members
        if _normalized_name(name)
    }
    totals: dict[str, dict[str, Any]] = {}

    for row in inspector_rows:
        if len(row) < 9:
            continue
        normalized = _normalized_name(row[1])
        if not normalized:
            continue
        canonical_member = canonical.get(normalized)
        bucket = totals.setdefault(
            normalized,
            {
                "community": (
                    canonical_member[0]
                    if canonical_member
                    else str(row[0] or "未分配社区")
                ),
                "name": (
                    canonical_member[1]
                    if canonical_member
                    else str(row[1]).strip()
                ),
                "counts": [0, 0, 0, 0, 0],
            },
        )
        counts = bucket["counts"]
        counts[0] += int(row[2] or 0)
        counts[1] += int(row[3] or 0)
        counts[2] += int(row[4] or 0)
        counts[3] += int(row[5] or 0)
        counts[4] += int(row[7] or 0)

    result = []
    for bucket in totals.values():
        total, unchecked, checked, completed, unable = bucket["counts"]
        result.append(
            (
                bucket["community"],
                bucket["name"],
                total,
                unchecked,
                checked,
                completed,
                calculate_ratio(completed, total),
                unable,
                calculate_ratio(max(completed - unable, 0), completed),
            )
        )
    result.sort(key=lambda row: (str(row[0] or ""), str(row[1] or "")))
    return result


async def rebuild_community_report_table(
    cur,
    inspector_table: str,
    community_table: str,
) -> None:
    """保留完整人员日报，只在重建社区表时应用当前岗位范围。"""
    positions = await get_configured_positions(
        cur,
        ONLINE_POSITION_CONFIG_KEY,
    )
    placeholders = ", ".join(["%s"] * len(positions))
    await cur.execute(f"TRUNCATE TABLE {community_table}")
    await cur.execute(
        f"""
        INSERT INTO {community_table}
            (社区, 数据总数, 未核查, 已核查, 已完成,
             核查完成率, 无法见底数, 核查见底率)
        SELECT
            report_row.社区,
            SUM(report_row.数据总数),
            SUM(report_row.未核查),
            SUM(report_row.已核查),
            SUM(report_row.已完成),
            CASE WHEN SUM(report_row.数据总数) > 0
                 THEN ROUND(
                    SUM(report_row.已完成) / SUM(report_row.数据总数),
                    2
                 )
                 ELSE 0 END,
            SUM(report_row.无法见底数),
            CASE WHEN SUM(report_row.已完成) > 0
                 THEN ROUND(
                    GREATEST(
                        SUM(report_row.已完成)
                        - SUM(report_row.无法见底数),
                        0
                    )
                    / SUM(report_row.已完成),
                    2
                 )
                 ELSE 0 END
        FROM {inspector_table} AS report_row
        LEFT JOIN OnlineData._grid_members AS person
          ON LOWER(TRIM(person.name)) = LOWER(TRIM(report_row.姓名))
        WHERE person.id IS NULL
           OR person.position IN ({placeholders})
        GROUP BY report_row.社区
        """,
        positions,
    )


async def rebuild_community_report_from_ledger(
    cur,
    inspector_table: str,
    community_table: str,
    report_date: str,
    parser_type: str,
) -> None:
    """直接从任务流水重建社区表，避免漏掉尚未填写核查人的任务。"""
    positions = await get_configured_positions(
        cur,
        ONLINE_POSITION_CONFIG_KEY,
    )
    placeholders = ", ".join(["%s"] * len(positions))
    await cur.execute(f"TRUNCATE TABLE {community_table}")
    await cur.execute(
        f"""
        INSERT INTO {community_table}
            (社区, 数据总数, 未核查, 已核查, 已完成,
             核查完成率, 无法见底数, 核查见底率)
        SELECT
            COALESCE(formal_community.name, ledger.community),
            COUNT(*),
            SUM(ledger.task_state = 'unchecked'),
            SUM(ledger.task_state = 'checked'),
            SUM(ledger.task_state = 'completed'),
            ROUND(SUM(ledger.task_state = 'completed') / COUNT(*), 2),
            SUM(ledger.unable_to_verify),
            CASE WHEN SUM(ledger.task_state = 'completed') > 0
                 THEN ROUND(
                    SUM(ledger.reached_bottom)
                    / SUM(ledger.task_state = 'completed'),
                    2
                 )
                 ELSE 0 END
        FROM _daily_task_ledger AS ledger
        LEFT JOIN OnlineData._community_aliases AS community_alias
          ON community_alias.alias = ledger.community
        LEFT JOIN OnlineData._communities AS formal_community
          ON formal_community.id = community_alias.community_id
        LEFT JOIN OnlineData._grid_members AS person
          ON LOWER(TRIM(person.name)) = LOWER(TRIM(ledger.inspector))
        WHERE ledger.report_date = %s
          AND ledger.parser_type = %s
          AND ledger.included = 1
          AND ledger.community <> ''
          AND ledger.community <> '社区'
          AND ledger.community <> '下发社区'
          AND (
              person.id IS NULL
              OR person.position IN ({placeholders})
          )
        GROUP BY COALESCE(formal_community.name, ledger.community)
        ORDER BY COALESCE(formal_community.name, ledger.community)
        """,
        (report_date, parser_type, *positions),
    )
    await cur.execute(
        f"""
        INSERT INTO {community_table}
            (社区, 数据总数, 未核查, 已核查, 已完成,
             核查完成率, 无法见底数, 核查见底率)
        SELECT
            report_row.社区,
            0, 0, 0, 0, 0, 0, 0
        FROM {inspector_table} AS report_row
        LEFT JOIN OnlineData._grid_members AS person
          ON LOWER(TRIM(person.name)) = LOWER(TRIM(report_row.姓名))
        LEFT JOIN {community_table} AS existing
          ON existing.社区 = report_row.社区
        WHERE report_row.数据总数 = 0
          AND existing.社区 IS NULL
          AND (
              person.id IS NULL
              OR person.position IN ({placeholders})
          )
        GROUP BY report_row.社区
        ORDER BY report_row.社区
        """,
        positions,
    )
