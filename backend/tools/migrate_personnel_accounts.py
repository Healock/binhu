"""把已有组织账号补建为人员，并迁移多社区关系。

默认只预览。名单和岗位覆盖文件均为私密输入，不得进入 Git。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from services.personnel_positions import normalize_position
from tools.import_users import read_rows, reject_duplicates


SOURCE_POSITION_MAP = {
    "社区民警": "社区民警",
    "所（队）领导": "所队领导",
    "所队领导": "所队领导",
}
OVERRIDE_POSITIONS = {"基础管控", "社区民警", "所队领导"}
EXPECTED_POSITION_COUNTS = {"社区民警": 13, "所队领导": 2, "基础管控": 2}
EXPECTED_MULTI_COMMUNITY_COUNT = 2


def _read_overrides(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("岗位覆盖文件必须是用户名到岗位的 JSON 对象")
    result: dict[str, str] = {}
    for raw_username, raw_position in payload.items():
        username = str(raw_username).strip()
        position = str(raw_position).strip()
        if not username or not position:
            raise ValueError("岗位覆盖文件不能包含空用户名或空岗位")
        try:
            normalized_position = normalize_position(position)
        except ValueError as exc:
            raise ValueError(f"岗位覆盖包含不支持的岗位：{position}") from exc
        if normalized_position not in OVERRIDE_POSITIONS:
            raise ValueError(f"岗位覆盖不允许设置为：{normalized_position}")
        result[username] = normalized_position
    return result


def _legacy_role(group_code: str) -> str:
    if group_code in {"admin", "internal_business"}:
        return "admin"
    if group_code == "global_viewer":
        return "leader"
    return "member"


def _validate_expected_preview(preview: list[dict[str, Any]]) -> None:
    positions = Counter(item["position"] for item in preview)
    multiple = sum(len(item["departments"]) > 1 for item in preview)
    if dict(positions) != EXPECTED_POSITION_COUNTS or multiple != EXPECTED_MULTI_COMMUNITY_COUNT:
        expected_total = sum(EXPECTED_POSITION_COUNTS.values())
        raise ValueError(
            "迁移预览数量不符合发布门槛："
            f"应为 {expected_total} 人且多社区 {EXPECTED_MULTI_COMMUNITY_COUNT} 人"
        )


async def _build_preview(cur, rows, overrides: dict[str, str]) -> list[dict[str, Any]]:
    await cur.execute(
        "SELECT id, username, member_id, role FROM _users"
    )
    users = {str(row[1]): row for row in await cur.fetchall()}
    member_owners = {
        int(row[2]): str(row[1])
        for row in users.values()
        if row[2] is not None
    }
    source_usernames = {item["username"] for item in rows}
    unknown_overrides = sorted(set(overrides) - source_usernames)
    if unknown_overrides:
        raise ValueError(
            f"岗位覆盖中有 {len(unknown_overrides)} 个用户名不在名单中"
        )
    await cur.execute("SELECT id, name FROM _grid_members")
    members = {str(row[1]): int(row[0]) for row in await cur.fetchall()}
    await cur.execute(
        """
        SELECT community.id, community.name, community.police_officers,
               department.id
        FROM _communities AS community
        JOIN _departments AS department
          ON department.community_id=community.id
         AND department.department_type='community'
        ORDER BY community.name, community.id
        """
    )
    police_departments: dict[str, list[int]] = {}
    departments: dict[int, dict[str, Any]] = {}
    for community_id, community_name, raw_officers, department_id in await cur.fetchall():
        departments[int(department_id)] = {
            "id": int(department_id),
            "name": str(community_name),
            "type": "community",
            "community_name": str(community_name),
        }
        try:
            officers = json.loads(raw_officers or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            officers = []
        if isinstance(officers, list):
            for officer in officers:
                name = str(officer).strip()
                if name:
                    police_departments.setdefault(name, []).append(int(department_id))

    await cur.execute(
        "SELECT id, name FROM _departments "
        "WHERE department_type='internal' AND is_active=1 ORDER BY id LIMIT 1"
    )
    internal = await cur.fetchone()
    if not internal:
        raise ValueError("内勤部门尚未初始化")
    internal_department = {
        "id": int(internal[0]),
        "name": str(internal[1]),
        "type": "internal",
        "community_name": None,
    }

    preview: list[dict[str, Any]] = []
    errors: list[str] = []
    for source_row, item in enumerate(rows, start=2):
        username = item["username"]
        account = users.get(username)
        if not account or account[2] is not None:
            continue
        position = overrides.get(username) or SOURCE_POSITION_MAP.get(item["position"])
        if not position:
            continue
        if str(account[3]) == "super_admin":
            errors.append(f"第 {source_row} 行：超级管理员账号不能自动关联")
            continue
        if position == "社区民警":
            department_ids = list(dict.fromkeys(police_departments.get(item["name"], [])))
            if not department_ids:
                errors.append(f"第 {source_row} 行：社区民警没有可核实的社区")
                continue
            selected_departments = [departments[value] for value in department_ids]
        else:
            selected_departments = [internal_department]

        await cur.execute(
            """
            SELECT permission_group.id, permission_group.code
            FROM _position_permission_group_links AS link
            JOIN _permission_groups AS permission_group
              ON permission_group.id=link.permission_group_id
            WHERE link.position=%s
            ORDER BY permission_group.sort_order, permission_group.id
            LIMIT 1
            """,
            (position,),
        )
        group = await cur.fetchone()
        if not group:
            errors.append(f"第 {source_row} 行：目标岗位没有默认权限组")
            continue
        existing_member_id = members.get(item["name"])
        owner = member_owners.get(existing_member_id) if existing_member_id else None
        if owner and owner != username:
            errors.append(f"第 {source_row} 行：同名人员已经关联其他账号")
            continue
        preview.append({
            "source_row": source_row,
            "user_id": int(account[0]),
            "name": item["name"],
            "position": position,
            "departments": selected_departments,
            "group_id": int(group[0]),
            "group_code": str(group[1]),
            "member_id": existing_member_id,
        })
    if errors:
        raise ValueError("迁移预览失败：\n- " + "\n- ".join(errors))
    return preview


async def _apply(cur, preview: list[dict[str, Any]]) -> None:
    from services.member_departments import (
        replace_member_departments,
        sync_community_police_compat,
    )

    for item in preview:
        member_id = item["member_id"]
        primary = item["departments"][0]
        if member_id is None:
            await cur.execute(
                """
                INSERT INTO _grid_members
                    (name, community, department_id, position)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    item["name"],
                    primary.get("community_name") or "",
                    primary["id"],
                    item["position"],
                ),
            )
            member_id = int(cur.lastrowid)
        else:
            await cur.execute(
                "UPDATE _grid_members SET position=%s WHERE id=%s",
                (item["position"], member_id),
            )
        await replace_member_departments(cur, member_id, item["departments"])
        await cur.execute(
            "DELETE FROM _user_permission_group_links WHERE user_id=%s",
            (item["user_id"],),
        )
        await cur.execute(
            """
            UPDATE _users
            SET member_id=%s, display_name=%s,
                group_assignment_mode='inherited',
                permission_group_id=%s, role=%s
            WHERE id=%s AND member_id IS NULL
            """,
            (
                member_id,
                item["name"],
                item["group_id"],
                _legacy_role(item["group_code"]),
                item["user_id"],
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"第 {item['source_row']} 行账号状态已变化，请重新预览")
    await sync_community_police_compat(cur)


async def run(args) -> None:
    from database import close_db, db_manager, init_db

    rows = read_rows(Path(args.file).resolve())
    reject_duplicates(rows)
    overrides = _read_overrides(args.overrides_file)
    await init_db()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            preview = await _build_preview(cur, rows, overrides)
            _validate_expected_preview(preview)
            positions = Counter(item["position"] for item in preview)
            multiple = sum(len(item["departments"]) > 1 for item in preview)
            print(
                "人员关联预览：共 " + str(len(preview)) + "；" +
                "，".join(f"{position} {count}" for position, count in sorted(positions.items())) +
                f"；多社区 {multiple}。"
            )
            if not args.apply:
                print("当前为预览模式，数据库未修改。")
                return
            await conn.begin()
            try:
                await _apply(cur, preview)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            print("人员、账号和部门关系迁移完成；密码和有效会话未修改。")
    finally:
        pool.release(conn)
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="补建已有账号的人员与部门关系")
    parser.add_argument("--file", required=True, help="私密 XLSX 名单路径")
    parser.add_argument(
        "--overrides-file",
        help="私密岗位覆盖 JSON；键为用户名，值为平台岗位",
    )
    parser.add_argument("--apply", action="store_true", help="正式写入数据库")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
