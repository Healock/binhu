"""人员与部门多对多关系及旧字段兼容。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from services.permissions import COMMUNITY_POSITIONS, INTERNAL_POSITIONS


MULTI_COMMUNITY_POSITIONS = {"社区民警"}


def normalize_department_ids(values: Iterable[Any] | None) -> list[int]:
    result: list[int] = []
    for raw_value in values or []:
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "所属部门编号无效") from exc
        if value <= 0:
            raise HTTPException(400, "所属部门编号无效")
        if value not in result:
            result.append(value)
    return result


async def resolve_departments(
    cur,
    position: str,
    department_ids: Iterable[Any] | None,
) -> list[dict[str, Any]]:
    """按岗位校验部门，返回按请求顺序排列的部门。"""
    requested = normalize_department_ids(department_ids)
    if position in INTERNAL_POSITIONS:
        await cur.execute(
            "SELECT id, name, department_type, NULL "
            "FROM _departments WHERE department_type='internal' "
            "AND is_active=1 ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(500, "内勤部门尚未初始化")
        return [_department_row(row)]

    if position in COMMUNITY_POSITIONS and not requested:
        raise HTTPException(400, "该岗位必须选择社区部门")
    if position not in MULTI_COMMUNITY_POSITIONS and len(requested) > 1:
        raise HTTPException(400, "该岗位只能选择一个社区部门")
    if not requested:
        return []

    placeholders = ", ".join(["%s"] * len(requested))
    await cur.execute(
        f"""
        SELECT department.id, department.name,
               department.department_type, community.name
        FROM _departments AS department
        LEFT JOIN _communities AS community
          ON community.id=department.community_id
        WHERE department.id IN ({placeholders})
          AND department.is_active=1
        """,
        requested,
    )
    rows = await cur.fetchall()
    by_id = {int(row[0]): _department_row(row) for row in rows}
    if len(by_id) != len(requested):
        raise HTTPException(400, "所属部门不存在或已停用")
    departments = [by_id[department_id] for department_id in requested]
    if any(item["type"] != "community" for item in departments):
        raise HTTPException(400, "该岗位只能选择社区部门")
    return departments


def _department_row(row) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "name": str(row[1] or ""),
        "type": str(row[2] or ""),
        "community_name": str(row[3] or "") or None,
    }


async def get_member_departments(cur, member_ids: Iterable[int]) -> dict[int, list[dict]]:
    ids = list(dict.fromkeys(int(value) for value in member_ids))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    await cur.execute(
        f"""
        SELECT link.member_id, department.id, department.name,
               department.department_type, community.name
        FROM _grid_member_department_links AS link
        JOIN _departments AS department ON department.id=link.department_id
        LEFT JOIN _communities AS community
          ON community.id=department.community_id
        WHERE link.member_id IN ({placeholders})
          AND department.is_active=1
        ORDER BY link.member_id, link.sort_order, department.name, department.id
        """,
        ids,
    )
    result: dict[int, list[dict]] = {}
    for row in await cur.fetchall():
        result.setdefault(int(row[0]), []).append(_department_row(row[1:]))
    return result


async def replace_member_departments(
    cur,
    member_id: int,
    departments: list[dict[str, Any]],
) -> None:
    await cur.execute(
        "DELETE FROM _grid_member_department_links WHERE member_id=%s",
        (member_id,),
    )
    if departments:
        await cur.executemany(
            "INSERT INTO _grid_member_department_links "
            "(member_id, department_id, sort_order) VALUES (%s, %s, %s)",
            [
                (member_id, department["id"], index)
                for index, department in enumerate(departments)
            ],
        )
    primary = departments[0] if departments else None
    await cur.execute(
        "UPDATE _grid_members SET department_id=%s, community=%s WHERE id=%s",
        (
            primary["id"] if primary else None,
            primary.get("community_name") or "" if primary else "",
            member_id,
        ),
    )


async def sync_community_police_compat(cur, community_ids: Iterable[int] | None = None) -> None:
    ids = normalize_department_ids(community_ids)
    where = ""
    params: list[int] = []
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        where = f"WHERE community.id IN ({placeholders})"
        params = ids
    await cur.execute(
        f"SELECT community.id FROM _communities AS community {where}",
        params,
    )
    target_ids = [int(row[0]) for row in await cur.fetchall()]
    for community_id in target_ids:
        await cur.execute(
            """
            SELECT DISTINCT member.name
            FROM _grid_member_department_links AS link
            JOIN _grid_members AS member ON member.id=link.member_id
            JOIN _departments AS department ON department.id=link.department_id
            WHERE department.community_id=%s
              AND member.position='社区民警'
            ORDER BY member.name
            """,
            (community_id,),
        )
        names = [str(row[0]).strip() for row in await cur.fetchall() if str(row[0]).strip()]
        await cur.execute(
            "UPDATE _communities SET police_officers=%s WHERE id=%s",
            (json.dumps(names, ensure_ascii=False), community_id),
        )


async def communities_for_member(cur, member_id: int) -> list[str]:
    await cur.execute(
        """
        SELECT community.name
        FROM _grid_member_department_links AS link
        JOIN _departments AS department ON department.id=link.department_id
        JOIN _communities AS community ON community.id=department.community_id
        WHERE link.member_id=%s AND department.is_active=1
        ORDER BY link.sort_order, community.name
        """,
        (member_id,),
    )
    return [str(row[0]).strip() for row in await cur.fetchall() if str(row[0]).strip()]
