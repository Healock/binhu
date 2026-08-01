"""人员岗位分类、固定在线口径及可配置的走访/备勤范围。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any


POSITION_OPTIONS = (
    "中队长",
    "基础管控",
    "自购房",
    "片长",
    "组长",
    "组员",
    "社区民警",
    "所队领导",
)
DEFAULT_SUMMARY_POSITIONS = ("组长", "组员")
ONLINE_SUMMARY_POSITIONS = ("组长", "组员")
POSITION_CATEGORIES = {
    "flow_work": ("组员", "组长", "自购房", "片长"),
    "internal_business": ("基础管控", "中队长"),
    "police_leadership": ("社区民警", "所队领导"),
}
ONLINE_POSITION_CONFIG_KEY = "online_summary_positions"
VISIT_POSITION_CONFIG_KEY = "visit_summary_positions"
WEEKEND_DUTY_POSITION_CONFIG_KEY = "weekend_duty_positions"
POSITION_CONFIG_KEYS = {
    ONLINE_POSITION_CONFIG_KEY,
    VISIT_POSITION_CONFIG_KEY,
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
}


def normalize_position(value: Any) -> str:
    position = str(value or "").strip()
    if position not in POSITION_OPTIONS:
        raise ValueError(f"不支持的岗位：{position or '空白'}")
    return position


def normalize_position_list(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        position = normalize_position(value)
        if position not in result:
            result.append(position)
    if not result:
        raise ValueError("至少选择一个参与统计的岗位")
    return result


def parse_position_config(value: Any) -> list[str]:
    if value in (None, ""):
        return list(DEFAULT_SUMMARY_POSITIONS)
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return list(DEFAULT_SUMMARY_POSITIONS)
    if not isinstance(parsed, list):
        return list(DEFAULT_SUMMARY_POSITIONS)
    try:
        return normalize_position_list(parsed)
    except ValueError:
        return list(DEFAULT_SUMMARY_POSITIONS)


def serialize_position_config(value: Any) -> str:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("统计岗位配置必须是岗位列表") from exc
    if not isinstance(parsed, list):
        raise ValueError("统计岗位配置必须是岗位列表")
    return json.dumps(
        normalize_position_list(parsed),
        ensure_ascii=False,
    )


def serialize_rental_position_config(value: Any) -> str:
    positions = json.loads(serialize_position_config(value))
    if "自购房" in positions:
        raise ValueError("自购房岗位使用单独的“自购房”汇总类型")
    return json.dumps(positions, ensure_ascii=False)


async def get_configured_positions(cur, config_key: str) -> list[str]:
    if config_key not in POSITION_CONFIG_KEYS:
        raise ValueError("未知的岗位统计配置")
    await cur.execute(
        "SELECT config_value FROM OnlineData._system_config "
        "WHERE config_key=%s",
        (config_key,),
    )
    row = await cur.fetchone()
    return parse_position_config(row[0] if row else None)


def normalized_person_name(value: Any) -> str:
    return str(value or "").strip().casefold()


async def get_known_personnel_positions(cur) -> dict[str, str]:
    await cur.execute(
        "SELECT name, position FROM OnlineData._grid_members "
        "WHERE TRIM(name) <> ''"
    )
    return {
        normalized_person_name(name): str(position or "组员").strip() or "组员"
        for name, position in await cur.fetchall()
    }


async def get_eligible_online_personnel(cur) -> dict[str, dict[str, str]]:
    """返回在线汇总允许统计的人员及其有效社区部门。"""
    await cur.execute(
        """
        SELECT member.name, member.position, community.name
        FROM OnlineData._grid_members AS member
        JOIN OnlineData._grid_member_department_links AS link
          ON link.member_id=member.id
        JOIN OnlineData._departments AS department
          ON department.id=link.department_id
         AND department.department_type='community'
        JOIN OnlineData._communities AS community
          ON community.id=department.community_id
        WHERE member.position IN ('组长', '组员')
          AND TRIM(member.name) <> ''
          AND TRIM(community.name) <> ''
        """
    )
    return {
        normalized_person_name(name): {
            "name": str(name).strip(),
            "position": str(position).strip(),
            "community": str(community).strip(),
        }
        for name, position, community in await cur.fetchall()
    }


async def get_personnel_scope(
    cur,
    config_key: str,
) -> tuple[set[str], dict[str, str]]:
    positions = set(await get_configured_positions(cur, config_key))
    known = await get_known_personnel_positions(cur)
    return positions, known


def person_is_in_scope(
    name: Any,
    selected_positions: set[str],
    known_positions: dict[str, str],
    *,
    include_unknown: bool = True,
) -> bool:
    position = known_positions.get(normalized_person_name(name))
    if position is None:
        return include_unknown
    return position in selected_positions


def filter_person_rows(
    rows: Iterable[Sequence[Any]],
    *,
    name_index: int,
    selected_positions: set[str],
    known_positions: dict[str, str],
    include_unknown: bool = True,
) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in rows
        if len(row) > name_index
        and person_is_in_scope(
            row[name_index],
            selected_positions,
            known_positions,
            include_unknown=include_unknown,
        )
    ]
