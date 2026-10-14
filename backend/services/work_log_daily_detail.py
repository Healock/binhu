"""滨湖网格每日工作明细 XLSX 生成。"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import copy
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from services.personnel_attendance import (
    get_attendance_context,
    is_member_on_duty,
    normalize_week_start,
    period_covers,
)
from services.personnel_positions import (
    VISIT_POSITION_CONFIG_KEY,
    get_configured_positions,
    get_known_personnel_positions,
    normalized_person_name,
)
from services.report_builders.summary import get_summary, _get_summary_types
from services.report_external_counts import merge_external_report_counts
from services.txdocs_statistics_monitor import get_txdocs_business_overlay
from services.visit_summary import (
    VISIT_CATEGORY_RENTAL,
    VISIT_CATEGORY_SELF_OWNED,
    get_visit_summary,
)


DEFAULT_RENTAL_TARGET = 10
DEFAULT_SELF_OWNED_TARGET = 15
DAILY_DETAIL_EXCLUDED_POSITIONS = {
    "社区民警",
    "所队领导",
    "中队长",
    "基础管控",
    "片长",
}
DEFAULT_POPULATION_COEFFICIENT = 10
MAX_POPULATION_COEFFICIENT = 999
MAX_VISIT_TARGET = 999
MIN_DATA_ROWS = 47
DATA_START_ROW = 4
LAST_COLUMN = 18
PREFERENCE_KEY_PREFIX = "work_log_daily_detail_targets:"
TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "work_log_daily_detail_template.xlsx"
)
DATA_FONT = Font(name="宋体", size=12)
DATA_ALIGNMENT = Alignment(
    horizontal="center",
    vertical="center",
    wrap_text=True,
)
DATA_BORDER = Border(
    left=Side(style="thin", color="000000"),
    right=Side(style="thin", color="000000"),
    top=Side(style="thin", color="000000"),
    bottom=Side(style="thin", color="000000"),
)


def normalize_targets(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}

    def target(key: str, default: int) -> int:
        try:
            normalized = int(source.get(key, default))
        except (TypeError, ValueError):
            normalized = default
        return min(MAX_VISIT_TARGET, max(0, normalized))

    return {
        "rental_target": target("rental_target", DEFAULT_RENTAL_TARGET),
        "self_owned_target": target(
            "self_owned_target",
            DEFAULT_SELF_OWNED_TARGET,
        ),
    }


def normalize_population_coefficient(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_POPULATION_COEFFICIENT
    return min(MAX_POPULATION_COEFFICIENT, max(0, value))


async def load_population_inputs(conn, business_date: date) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT community.id, community.name,
                   COALESCE(input.added_count, NULL),
                   COALESCE(input.cancelled_count, NULL),
                   COALESCE(input.revision, 0)
            FROM _communities AS community
            LEFT JOIN _work_log_daily_detail_population AS input
              ON input.community_id=community.id
             AND input.business_date=%s
            WHERE community.is_active=1
            ORDER BY community.id
            """,
            (business_date,),
        )
        fetchall = getattr(cur, "fetchall", None)
        rows = await fetchall() if fetchall else []
    return [
        {
            "community_id": int(row[0]),
            "community": str(row[1]).strip(),
            "added": None if row[2] is None else int(row[2]),
            "cancelled": None if row[3] is None else int(row[3]),
            "revision": int(row[4] or 0),
        }
        for row in rows
    ]


async def save_population_inputs(conn, business_date: date, values: list[dict[str, Any]], user_id: int) -> dict[str, int]:
    """原子保存一日社区人工数值，revision 必须与客户端一致。"""
    normalized = []
    for item in values:
        community_id = int(item["community_id"])
        revision = int(item.get("revision") or 0)
        def count(key: str):
            value = item.get(key)
            if value in (None, ""):
                return None
            value = int(value)
            if value < 0:
                raise ValueError("流动人口新增和注销必须是非负整数")
            return value
        normalized.append((community_id, count("added"), count("cancelled"), revision))
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            community_ids = sorted({item[0] for item in normalized})
            if community_ids:
                placeholders = ",".join(["%s"] * len(community_ids))
                await cur.execute(
                    f"SELECT id FROM _communities WHERE is_active=1 AND id IN ({placeholders})",
                    community_ids,
                )
                valid_ids = {int(row[0]) for row in await cur.fetchall()}
                if valid_ids != set(community_ids):
                    raise ValueError("包含无效或已停用的社区，请重新加载")
            for community_id, added, cancelled, revision in normalized:
                await cur.execute(
                    """
                    SELECT revision
                    FROM _work_log_daily_detail_population
                    WHERE business_date=%s AND community_id=%s
                    FOR UPDATE
                    """,
                    (business_date, community_id),
                )
                current = await cur.fetchone()
                expected_revision = max(0, revision)
                if current is None:
                    if expected_revision != 0:
                        raise RuntimeError("流动人口数据已被其他管理员修改，请重新加载")
                    await cur.execute(
                        """
                        INSERT INTO _work_log_daily_detail_population
                            (business_date, community_id, added_count, cancelled_count, revision, updated_by)
                        VALUES (%s, %s, %s, %s, 1, %s)
                        """,
                        (business_date, community_id, added, cancelled, user_id),
                    )
                    continue
                if int(current[0]) != expected_revision:
                    raise RuntimeError("流动人口数据已被其他管理员修改，请重新加载")
                await cur.execute(
                    """
                    UPDATE _work_log_daily_detail_population
                    SET added_count=%s, cancelled_count=%s,
                        revision=revision + 1, updated_by=%s
                    WHERE business_date=%s AND community_id=%s
                    """,
                    (added, cancelled, user_id, business_date, community_id),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"count": len(normalized)}


async def load_target_preferences(conn, user_id: int) -> dict[str, int]:
    key = f"{PREFERENCE_KEY_PREFIX}{int(user_id)}"
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT config_value FROM _system_config WHERE config_key=%s",
            (key,),
        )
        row = await cur.fetchone()
    if not row or not row[0]:
        return {
            **normalize_targets({}),
            "population_coefficient": DEFAULT_POPULATION_COEFFICIENT,
        }
    try:
        value = json.loads(row[0])
        return {
            **normalize_targets(value),
            "population_coefficient": normalize_population_coefficient(
                value.get("population_coefficient") if isinstance(value, dict) else None,
            ),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            **normalize_targets({}),
            "population_coefficient": DEFAULT_POPULATION_COEFFICIENT,
        }


async def save_target_preferences(
    conn,
    user_id: int,
    targets: dict[str, int],
) -> None:
    key = f"{PREFERENCE_KEY_PREFIX}{int(user_id)}"
    normalized = {
        **normalize_targets(targets),
        "population_coefficient": normalize_population_coefficient(
            targets.get("population_coefficient"),
        ),
    }
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO _system_config (config_key, config_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
            """,
            (key, json.dumps(normalized, ensure_ascii=False)),
        )


async def _area_labels(conn) -> tuple[dict[str, str], dict[str, int]]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT area.name,
                   GROUP_CONCAT(DISTINCT member.name
                       ORDER BY member.name SEPARATOR '、')
            FROM _areas AS area
            LEFT JOIN _area_leader_links AS link ON link.area_id=area.id
            LEFT JOIN _grid_members AS member ON member.id=link.member_id
            GROUP BY area.id, area.name
            ORDER BY area.id
            """
        )
        rows = await cur.fetchall()
    labels: dict[str, str] = {}
    order: dict[str, int] = {}
    for index, (area_name, leaders) in enumerate(rows):
        name = str(area_name or "").strip()
        if not name:
            continue
        leader_text = str(leaders or "").strip()
        labels[name] = f"{name}\n{leader_text}" if leader_text else name
        order[name] = index
    return labels, order


async def _load_roster(conn, business_date: date) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT member.id, member.name, member.position, member.status,
                   SUBSTRING_INDEX(GROUP_CONCAT(DISTINCT community.id
                       ORDER BY link.sort_order, community.name SEPARATOR ','), ',', 1),
                   GROUP_CONCAT(DISTINCT community.name
                       ORDER BY link.sort_order, community.name SEPARATOR '、'),
                   GROUP_CONCAT(DISTINCT COALESCE(area.name, leader_area.name)
                       ORDER BY COALESCE(area.id, leader_area.id) SEPARATOR '、'),
                   GROUP_CONCAT(DISTINCT department.name
                       ORDER BY link.sort_order, department.name SEPARATOR '、'),
                   member_area.id, member_area.name
            FROM _grid_members AS member
            LEFT JOIN _grid_member_department_links AS link
              ON link.member_id=member.id
            LEFT JOIN _departments AS department
              ON department.id=link.department_id
            LEFT JOIN _communities AS community
              ON community.id=department.community_id
            LEFT JOIN _areas AS area ON area.id=community.area_id
            LEFT JOIN _area_leader_links AS leader_link
              ON leader_link.member_id=member.id
            LEFT JOIN _areas AS leader_area ON leader_area.id=leader_link.area_id
            LEFT JOIN _areas AS member_area ON member_area.id=member.area_id
            WHERE TRIM(member.name) <> '' AND member.status <> '离岗'
            GROUP BY member.id, member.name, member.position, member.status,
                     member_area.id, member_area.name
            """
        )
        rows = await cur.fetchall()

    positions = {
        str(row[2] or "").strip() or "未设置岗位"
        for row in rows
    }
    async with conn.cursor() as cur:
        attendance = await get_attendance_context(
            cur,
            start_date=business_date,
            end_date=business_date,
            selected_positions=positions,
        )
    area_labels, area_order = await _area_labels(conn)
    missing_week = normalize_week_start(business_date) in attendance.get(
        "missing_week_starts",
        set(),
    )

    roster: list[dict[str, Any]] = []
    for raw_row in rows:
        if len(raw_row) >= 10:
            member_id, name, position, status, community_id, communities, areas, departments, member_area_id, member_area_name = raw_row[:10]
        elif len(raw_row) >= 8:
            member_id, name, position, status, community_id, communities, areas, departments = raw_row[:8]
            member_area_id, member_area_name = None, ""
        else:
            member_id, name, position, status, communities, areas, departments = raw_row[:7]
            community_id = None
            member_area_id, member_area_name = None, ""
        member_name = str(name).strip()
        member_position = str(position or "").strip() or "未设置岗位"
        if member_position in DAILY_DETAIL_EXCLUDED_POSITIONS:
            continue
        community = str(communities or "").split("、", 1)[0].strip()
        area = str(areas or "").split("、", 1)[0].strip()
        department = str(departments or "").split("、", 1)[0].strip()
        if member_position == "自购房":
            area = str(member_area_name or "").strip()
            community_id = None
            community = "自购房" if area else ""
            department = ""
        if member_position == "管家码":
            # 历史关联即使未被清理，也不能让管家码进入辖区或社区统计。
            community_id = None
            community = ""
            area = ""
            department = ""
        attendance_member = attendance.get("members", {}).get(member_name, {
            "id": int(member_id),
            "name": member_name,
            "community": community or "未分配社区",
            "communities": [community] if community else [],
            "position": member_position,
        })
        periods = attendance.get("periods", {}).get(int(member_id), [])
        if period_covers(business_date, periods):
            attendance_text = "否"
        elif (
            business_date.weekday() >= 5
            and member_position in attendance.get("weekend_duty_positions", set())
            and missing_week
        ):
            attendance_text = "未排班"
        else:
            attendance_text = (
                "是"
                if is_member_on_duty(attendance_member, business_date, attendance)
                else "否"
            )

        if area:
            group_key = f"area:{area}"
            group_label = area_labels.get(area, area)
            group_order = area_order.get(area, 999)
        else:
            group_key = f"internal:{member_position}"
            group_label = member_position or department or "其他"
            group_order = 1000

        roster.append({
            "id": int(member_id),
            "name": member_name,
            "position": member_position,
            "community": community,
            "community_id": int(community_id) if community_id else None,
            "area": area,
            "department": department,
            "group_key": group_key,
            "group_label": group_label,
            "group_order": group_order,
            "attendance": attendance_text,
        })

    position_order = {
        "片长": 0,
        "社区民警": 1,
        "组长": 2,
        "组员": 3,
        "自购房": 4,
        "基础管控": 10,
        "中队长": 11,
        "所队领导": 12,
        "管家码": 13,
    }
    roster.sort(key=lambda item: (
        int(item["group_order"]),
        str(item["group_label"]),
        1 if str(item["position"]) == "自购房" else 0,
        str(item["community"]),
        position_order.get(str(item["position"]), 50),
        str(item["name"]),
    ))
    return roster


def _summary_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = (payload.get("inspector") or {}).get("data") or []
    return {
        normalized_person_name(row.get("姓名")): row
        for row in rows
        if normalized_person_name(row.get("姓名"))
    }


def _normalize_daily_detail_community(value: Any) -> str:
    """Normalize formal community labels and common committee suffix aliases."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\s\u3000]+", "", text).casefold()
    text = re.sub(r"(?:居民委员会|居委会)$", "", text)
    return re.sub(r"社区$", "", text)


def _summary_by_member(
    payload: dict[str, Any],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """按社区和姓名建立日报候选索引，重复记录不得静默覆盖。"""
    rows = (payload.get("inspector") or {}).get("data") or []
    by_member: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = normalized_person_name(row.get("姓名"))
        if not name:
            continue
        community = _normalize_daily_detail_community(row.get("社区"))
        by_member.setdefault((community, name), []).append(row)
        by_name.setdefault(name, []).append(row)
    return by_member, by_name


def _combine_instruction_rows(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum one unambiguous row per community and recalculate its aggregate rate."""
    by_community: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        community = _normalize_daily_detail_community(row.get("社区"))
        by_community.setdefault(community, []).append(row)
    if not rows or any(len(candidates) != 1 for candidates in by_community.values()):
        return None

    unique_rows = [candidates[0] for candidates in by_community.values()]
    combined: dict[str, Any] = {}
    for field in ("数据总数", "未核查", "已核查", "已完成", "无法见底数"):
        values = [row.get(field) for row in unique_rows]
        present = [value for value in values if value is not None]
        if present:
            combined[field] = sum(int(value or 0) for value in present)
    if len(unique_rows) == 1:
        combined["核查见底率"] = unique_rows[0].get("核查见底率")
    elif all(row.get("无法见底数") is not None for row in unique_rows):
        denominator = combined.get("已完成", 0) + combined.get("无法见底数", 0)
        combined["核查见底率"] = combined.get("已完成", 0) / denominator if denominator else 0
    return combined


async def build_daily_detail_data(
    conn,
    business_date: date,
    *,
    rental_target: int,
    self_owned_target: int,
    population_coefficient: int = DEFAULT_POPULATION_COEFFICIENT,
) -> list[dict[str, Any]]:
    roster = [
        member
        for member in await _load_roster(conn, business_date)
        if str(member.get("position") or "") not in DAILY_DETAIL_EXCLUDED_POSITIONS
    ]
    coefficient = normalize_population_coefficient(population_coefficient)
    population_rows = await load_population_inputs(conn, business_date)
    population_by_community = {
        row["community_id"]: row for row in population_rows
    }
    duty_counts: dict[int, int] = {}
    for member in roster:
        if member["position"] in {"组长", "组员"} and member["attendance"] == "是" and member.get("community_id"):
            community_id = int(member["community_id"])
            duty_counts[community_id] = duty_counts.get(community_id, 0) + 1

    async with conn.cursor() as cur:
        known_positions = await get_known_personnel_positions(cur)
        rental_positions = set(await get_configured_positions(
            cur,
            VISIT_POSITION_CONFIG_KEY,
        )) - {"自购房", "管家码"}

    rental = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_RENTAL,
        selected_positions=rental_positions,
        known_positions=known_positions,
    )
    self_owned = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_SELF_OWNED,
        selected_positions={"自购房"},
        known_positions=known_positions,
    )
    online = await get_summary(business_date.isoformat())
    # Reuse the export request's OnlineData connection; never acquire a
    # second connection from that pool while the request holds this one.
    async with conn.cursor() as cur:
        summary_types = await _get_summary_types(cur)
        await cur.execute("SELECT id FROM _txdocs_monitor_target WHERE enabled=1 ORDER BY id")
        configured_source_ids = [int(row[0]) for row in await cur.fetchall()]
    external = await get_txdocs_business_overlay(
        business_date.isoformat(), business_date.isoformat(), summary_types, None,
        configured_source_ids=configured_source_ids
    )
    online = merge_external_report_counts(online, external, parser_type="总汇总表")

    rental_rows = _summary_by_name(rental)
    self_owned_rows = _summary_by_name(self_owned)
    online_by_member, online_by_name = (
        _summary_by_member(online) if online.get("exists") else ({}, {})
    )
    roster_name_counts: dict[str, int] = {}
    for roster_member in roster:
        roster_name = normalized_person_name(roster_member.get("name"))
        roster_name_counts[roster_name] = roster_name_counts.get(roster_name, 0) + 1

    result: list[dict[str, Any]] = []
    for member in roster:
        normalized_name = normalized_person_name(member["name"])
        position = str(member["position"])
        if position == "自购房":
            visit_row = self_owned_rows.get(normalized_name)
            target: int | None = self_owned_target
        elif position in rental_positions:
            visit_row = rental_rows.get(normalized_name)
            target = rental_target
        else:
            visit_row = None
            target = None

        if roster_name_counts.get(normalized_name) == 1:
            task_row = _combine_instruction_rows(online_by_name.get(normalized_name, []))
        else:
            member_rows = online_by_member.get(
                (_normalize_daily_detail_community(member.get("community")), normalized_name),
                [],
            )
            task_row = member_rows[0] if len(member_rows) == 1 else None
        absent = member["attendance"] == "否"
        population = population_by_community.get(member.get("community_id"), {})
        population_target = duty_counts.get(member.get("community_id"), 0) * coefficient
        if position == "管家码":
            population_target = None
        result.append({
            **member,
            "population_target": population_target,
            "population_added": population.get("added"),
            "population_cancelled": population.get("cancelled"),
            "population_revision": population.get("revision", 1),
            "visit_target": None if position == "管家码" else target,
            "visits": (
                int(visit_row.get("走访户数") or 0)
                if visit_row is not None
                else None
            ),
            "changed": int(visit_row.get("变更") or 0) if visit_row is not None else None,
            "cancelled": int(visit_row.get("注销") or 0) if visit_row is not None else None,
            "ratings": int(visit_row.get("星级评定数") or 0) if visit_row is not None else None,
            "average_changes": float(visit_row.get("户均变动数") or 0) if visit_row is not None else None,
            "checked_instructions": (
                int(task_row.get("已核查") or 0)
                + int(task_row.get("已完成") or 0)
                if task_row is not None
                else None
            ),
            "completed_instructions": (
                int(task_row.get("已完成") or 0)
                if task_row is not None
                else None
            ),
            "instruction_total": int(task_row.get("数据总数") or 0) if task_row is not None else None,
            "bottom_rate": task_row.get("核查见底率") if task_row is not None else None,
            "unable_instructions": (
                int(task_row["无法见底数"] or 0)
                if task_row is not None and task_row.get("无法见底数") is not None
                else None
            ),
        })
        if absent:
            result[-1].update({
                "population_target": None,
                "population_added": None,
                "population_cancelled": None,
                "visit_target": None,
                "visits": None,
                "changed": None,
                "cancelled": None,
                "average_changes": None,
                "checked_instructions": None,
                "completed_instructions": None,
                "instruction_total": None,
                "bottom_rate": None,
                "unable_instructions": None,
            })
    return result


def _copy_row_style(ws, source_row: int, target_row: int) -> None:
    ws.row_dimensions[target_row].height = ws.row_dimensions[source_row].height
    for column in range(1, LAST_COLUMN + 1):
        source = ws.cell(source_row, column)
        target = ws.cell(target_row, column)
        if source.has_style:
            target._style = copy(source._style)
        target.number_format = source.number_format
        target.alignment = copy(source.alignment)
        target.protection = copy(source.protection)


def _merge_groups(ws, rows: list[dict[str, Any]]) -> None:
    def merge_column(column: int, key_builder) -> None:
        start = DATA_START_ROW
        current = None
        for offset, item in enumerate(rows):
            row_number = DATA_START_ROW + offset
            key = key_builder(item)
            if current is None:
                current = key
                start = row_number
                continue
            if key == current:
                continue
            if current and row_number - 1 > start:
                ws.merge_cells(
                    start_row=start,
                    start_column=column,
                    end_row=row_number - 1,
                    end_column=column,
                )
            current = key
            start = row_number
        if current and rows and DATA_START_ROW + len(rows) - 1 > start:
            ws.merge_cells(
                start_row=start,
                start_column=column,
                end_row=DATA_START_ROW + len(rows) - 1,
                end_column=column,
            )

    merge_column(2, lambda item: item.get("group_key"))
    merge_column(
        3,
        lambda item: (
            f"{item.get('group_key')}:{item.get('community')}"
            if item.get("community")
            else None
        ),
    )
    for column in (6, 7, 8):
        merge_column(
            column,
            lambda item: (
                (item.get("community_id"), item.get("attendance"))
                if item.get("community_id")
                else None
            ),
        )


def _ordered_daily_detail_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each area's self-owned row after all community rows."""
    groups: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []
    for item in rows:
        key = str(item.get("group_key") or "")
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(item)
    ordered: list[dict[str, Any]] = []
    for key in group_order:
        regular = [
            item for item in groups[key]
            if str(item.get("position") or "") != "自购房"
        ]
        self_owned = [
            item for item in groups[key]
            if str(item.get("position") or "") == "自购房"
        ]
        ordered.extend(regular)
        ordered.extend(self_owned)
    return ordered


def build_daily_detail_workbook(
    business_date: date,
    rows: list[dict[str, Any]],
) -> tuple[bytes, str]:
    rows = _ordered_daily_detail_rows(rows)
    workbook = load_workbook(TEMPLATE_PATH)
    worksheet = workbook["Sheet1"]
    final_data_rows = max(MIN_DATA_ROWS, len(rows))
    final_row = DATA_START_ROW + final_data_rows - 1
    total_row = final_row + 1
    if final_row > worksheet.max_row:
        base_max_row = worksheet.max_row
        extra = final_row - base_max_row
        worksheet.insert_rows(base_max_row + 1, extra)
        for row_number in range(base_max_row + 1, final_row + 1):
            _copy_row_style(worksheet, 4, row_number)

    for merged in list(worksheet.merged_cells.ranges):
        if merged.min_row >= DATA_START_ROW:
            worksheet.unmerge_cells(str(merged))

    worksheet["A1"] = f"滨湖网格工作每日明细（{business_date.month}月{business_date.day}号）"
    no_fill = PatternFill(fill_type=None)
    for row in worksheet.iter_rows(
        min_row=DATA_START_ROW,
        max_row=total_row,
        min_col=1,
        max_col=LAST_COLUMN,
    ):
        for cell in row:
            cell.value = None
            cell.fill = copy(no_fill)
            cell.font = copy(DATA_FONT)
            cell.alignment = copy(DATA_ALIGNMENT)
            cell.border = copy(DATA_BORDER)
            cell.number_format = "General"

    for index, item in enumerate(rows, start=1):
        row_number = DATA_START_ROW + index - 1
        worksheet.cell(row_number, 1, index)
        worksheet.cell(row_number, 2, item["group_label"])
        worksheet.cell(row_number, 3, item["community"] or None)
        worksheet.cell(row_number, 4, item["name"])
        worksheet.cell(row_number, 5, item["attendance"])
        population_target = item.get("population_target")
        worksheet.cell(row_number, 6, "/" if population_target == 0 else population_target)
        worksheet.cell(row_number, 7, "/" if population_target == 0 else item.get("population_added"))
        worksheet.cell(row_number, 8, "/" if population_target == 0 else item.get("population_cancelled"))
        worksheet.cell(row_number, 9, item.get("visit_target"))
        worksheet.cell(row_number, 10, item.get("visits"))
        worksheet.cell(row_number, 11, item.get("changed", item.get("ratings")))
        worksheet.cell(row_number, 12, item.get("cancelled", item.get("checked_instructions")))
        worksheet.cell(row_number, 13, item.get("average_changes", item.get("completed_instructions")))
        worksheet.cell(row_number, 14, item.get("instruction_total"))
        checked = item.get("checked_instructions")
        completed = item.get("completed_instructions")
        worksheet.cell(row_number, 15, checked)
        worksheet.cell(row_number, 16, completed)
        worksheet.cell(row_number, 17, item.get("bottom_rate"))
        if item.get("position") == "管家码":
            for column in range(6, 19):
                worksheet.cell(row_number, column).value = None
        if item.get("attendance") == "否" and item.get("position") != "管家码":
            for column in range(6, 18):
                worksheet.cell(row_number, column).value = "/"
        if population_target and item.get("population_added") is not None and item.get("population_cancelled") is not None:
            ratio = (item["population_added"] + item["population_cancelled"]) / population_target
            fill = PatternFill("solid", fgColor=("F4CCCC" if ratio < 0.5 else "FFF2CC" if ratio < 1 else "D9EAD3"))
            worksheet.cell(row_number, 7).fill = fill
            worksheet.cell(row_number, 8).fill = fill

    _merge_groups(worksheet, rows)
    worksheet.cell(total_row, 1, "合计")
    worksheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=5)
    for column in range(6, 18):
        values = [worksheet.cell(row, column).value for row in range(DATA_START_ROW, final_row + 1)]
        numbers = [value for value in values if isinstance(value, (int, float))]
        worksheet.cell(total_row, column, sum(numbers) if numbers else None)
    counted_rows = [item for item in rows
                    if item.get("attendance") != "否" and item.get("position") != "管家码"
                    and item.get("instruction_total") is not None]
    # Ground rate is completed / (completed + unable), not checked / total.
    # Without a trustworthy denominator, leave the aggregate blank.
    if counted_rows and all(item.get("unable_instructions") is not None for item in counted_rows):
        completed = sum(int(item.get("completed_instructions") or 0) for item in counted_rows)
        unable = sum(int(item["unable_instructions"] or 0) for item in counted_rows)
        worksheet.cell(total_row, 17, completed / (completed + unable) if completed + unable else 0)
    else:
        worksheet.cell(total_row, 17).value = None
    for row_number in range(DATA_START_ROW, total_row + 1):
        worksheet.cell(row_number, 13).number_format = "0.00"
        worksheet.cell(row_number, 17).number_format = "0.00%"

    for cell in worksheet[total_row]:
        cell.font = Font(name="宋体", size=12, bold=True)
    worksheet.column_dimensions["M"].number_format = "0.00"
    worksheet.column_dimensions["Q"].number_format = "0.00%"
    worksheet.print_area = f"A1:R{total_row}"
    worksheet.sheet_view.showGridLines = False

    output = BytesIO()
    workbook.save(output)
    filename = (
        f"{business_date.strftime('%m%d')}滨湖网格工作每日明细.xlsx"
    )
    return output.getvalue(), filename


async def build_daily_detail_export(
    conn,
    business_date: date,
    *,
    rental_target: int,
    self_owned_target: int,
    population_coefficient: int = DEFAULT_POPULATION_COEFFICIENT,
) -> tuple[bytes, str, int]:
    rows = await build_daily_detail_data(
        conn,
        business_date,
        rental_target=rental_target,
        self_owned_target=self_owned_target,
        population_coefficient=population_coefficient,
    )
    content, filename = build_daily_detail_workbook(business_date, rows)
    return content, filename, len(rows)
