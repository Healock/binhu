"""取消历史「内勤」部门并归位人员。

本维护工具只处理 PlatformData 中的人员部门关系，不创建部门树，也不猜测
人员归属。目标部门必须由管理员先在部门管理中建立。生产维护窗口固定执行::

    python -m migrations.internal_department_retirement measure
    python -m migrations.internal_department_retirement migrate --apply \\
      --run-id <run-id> --backup-manifest <manifest> \\
      --expect-internal-members <count> --confirm-remove-internal
    python -m migrations.internal_department_retirement verify

``measure``、``verify`` 只读；``migrate --apply`` 在单个事务中更新部门关系、
兼容字段并删除空的历史 internal 部门。真实 MySQL、备份和生产数据不在本地验证。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiomysql

from config import settings


INTERNAL_TYPE = "internal"
TARGET_DEPARTMENTS = (
    "社区警务队",
    "东片",
    "中片",
    "西片",
    "基础管控",
    "管家码",
    "综合指挥室",
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")

# 这些是已确认的人员归位；岗位用于防止同名人员被误迁移。
NAMED_TARGETS: dict[str, tuple[str, set[str]]] = {
    "张航": ("社区警务队", {"所队领导"}),
    "李静": ("社区警务队", {"队领导", "所队领导"}),
    "熊朝良": ("东片", {"片长"}),
    "褚寿生": ("中片", {"片长"}),
    "蔡泉波": ("西片", {"片长"}),
    "蒋峰": ("社区警务队", {"中队长"}),
    "周颂阳": ("基础管控", {"基础管控"}),
    "朱益兵": ("基础管控", {"基础管控"}),
    "钱程浩": ("基础管控", {"基础管控"}),
    "计志威": ("基础管控", {"基础管控"}),
    "桑晓晨": ("管家码", {"管家码"}),
    "陈忠": ("综合指挥室", {"主任", "所队领导"}),
    "高泽鑫": ("综合指挥室", {""}),
}
UNASSIGNED_MEMBERS = {"金润逸"}


async def open_connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_PLATFORM_DB,
        autocommit=False,
        charset="utf8mb4",
    )


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


async def _internal_departments(cur) -> list[dict[str, Any]]:
    await cur.execute(
        "SELECT id,name,is_active FROM _departments "
        "WHERE department_type=%s ORDER BY id",
        (INTERNAL_TYPE,),
    )
    return [
        {"id": int(row[0]), "name": str(row[1] or ""), "is_active": bool(row[2])}
        for row in await cur.fetchall()
    ]


async def _internal_members(cur, department_ids: list[int]) -> list[dict[str, Any]]:
    if not department_ids:
        return []
    placeholders = ",".join(["%s"] * len(department_ids))
    await cur.execute(
        f"""
        SELECT DISTINCT member.id, member.name, member.position, member.area_id,
               member.department_id, department.name
        FROM _grid_members AS member
        LEFT JOIN _grid_member_department_links AS link
          ON link.member_id=member.id AND link.department_id IN ({placeholders})
        LEFT JOIN _departments AS department
          ON department.id=member.department_id
        WHERE member.department_id IN ({placeholders})
           OR link.department_id IS NOT NULL
        ORDER BY member.position, member.name, member.id
        """,
        tuple(department_ids) * 2,
    )
    return [
        {
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "position": str(row[2] or ""),
            "area_id": int(row[3]) if row[3] is not None else None,
            "department_id": int(row[4]) if row[4] is not None else None,
            "department_name": str(row[5] or ""),
        }
        for row in await cur.fetchall()
    ]


async def _target_inventory(cur) -> dict[str, list[dict[str, Any]]]:
    placeholders = ",".join(["%s"] * len(TARGET_DEPARTMENTS))
    await cur.execute(
        f"""
        SELECT id,name,department_type,is_active,area_id
        FROM _departments
        WHERE name IN ({placeholders})
        ORDER BY name,id
        """,
        TARGET_DEPARTMENTS,
    )
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in await cur.fetchall():
        result[str(row[1])].append({
            "id": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "is_active": bool(row[3]),
            "area_id": int(row[4]) if row[4] is not None else None,
        })
    return dict(result)


async def _shared_link_members(cur, internal_ids: list[int]) -> list[dict[str, Any]]:
    if not internal_ids:
        return []
    placeholders = ",".join(["%s"] * len(internal_ids))
    await cur.execute(
        f"""
        SELECT DISTINCT member.id, member.name, department.name
        FROM _grid_members AS member
        JOIN _grid_member_department_links AS internal_link
          ON internal_link.member_id=member.id
         AND internal_link.department_id IN ({placeholders})
        JOIN _grid_member_department_links AS other_link
          ON other_link.member_id=member.id
         AND other_link.department_id NOT IN ({placeholders})
        JOIN _departments AS department ON department.id=other_link.department_id
        ORDER BY member.id, department.name
        """,
        tuple(internal_ids) * 2,
    )
    return [
        {"id": int(row[0]), "name": str(row[1] or ""), "department": str(row[2] or "")}
        for row in await cur.fetchall()
    ]


async def _child_references(cur, internal_ids: list[int]) -> list[dict[str, Any]]:
    if not internal_ids:
        return []
    placeholders = ",".join(["%s"] * len(internal_ids))
    await cur.execute(
        f"SELECT child.id,child.name,parent.name "
        f"FROM _departments AS child "
        f"JOIN _departments AS parent ON parent.id=child.parent_id "
        f"WHERE child.parent_id IN ({placeholders}) ORDER BY child.id",
        tuple(internal_ids),
    )
    return [
        {"id": int(row[0]), "name": str(row[1] or ""), "parent": str(row[2] or "")}
        for row in await cur.fetchall()
    ]


def _target_for_member(member: dict[str, Any]) -> str | None:
    if member["name"] in UNASSIGNED_MEMBERS:
        return "__unassigned__"
    named = NAMED_TARGETS.get(member["name"])
    if named:
        target, positions = named
        if member["position"] in positions:
            return target
        return None
    if member["position"] == "自购房" and member["area_id"] is not None:
        return f"__area__:{member['area_id']}"
    return None


def _unmapped_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [member for member in members if _target_for_member(member) is None]


async def summarize(cur, command: str = "measure") -> dict[str, Any]:
    departments = await _internal_departments(cur)
    internal_ids = [item["id"] for item in departments]
    members = await _internal_members(cur, internal_ids)
    targets = await _target_inventory(cur)
    shared_links = await _shared_link_members(cur, internal_ids)
    child_references = await _child_references(cur, internal_ids)
    by_position: dict[str, int] = defaultdict(int)
    for member in members:
        by_position[member["position"]] += 1
    return {
        "command": command,
        "internal_departments": departments,
        "internal_member_count": len(members),
        "internal_members_by_position": dict(sorted(by_position.items())),
        "internal_members": members,
        "unmapped_members": _unmapped_members(members),
        "shared_department_links": shared_links,
        "child_references": child_references,
        "target_departments": targets,
        "backup_scope": "PlatformData (_departments, _grid_members, _grid_member_department_links)",
        "external_tencent_materials": "untouched",
    }


async def run_measure() -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await summarize(cur)
        await conn.rollback()
        return result
    finally:
        await close_connection(conn)


async def _resolve_targets(cur, members: list[dict[str, Any]]) -> dict[int, int | None]:
    inventory = await _target_inventory(cur)
    resolved: dict[int, int] = {}
    for member in members:
        target = _target_for_member(member)
        if target == "__unassigned__":
            resolved[member["id"]] = None
            continue
        if target and target.startswith("__area__:"):
            area_id = int(target.split(":", 1)[1])
            matches = [
                item for items in inventory.values() for item in items
                if item["type"] == "unit" and item["is_active"] and item["area_id"] == area_id
            ]
            target_name = f"area_id={area_id}"
        else:
            target_name = str(target or "")
            matches = [
                item for item in inventory.get(target_name, [])
                if item["type"] == "unit" and item["is_active"]
            ]
        if len(matches) != 1:
            raise RuntimeError(
                f"人员 {member['name']} 的目标部门 {target_name} 不存在、未启用或不唯一"
            )
        resolved[member["id"]] = matches[0]["id"]
    return resolved


async def run_apply(
    *,
    run_id: str,
    backup_manifest: str,
    expected_internal_members: int,
    confirm_remove_internal: bool,
) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeError("run-id 必须是 8-64 位字母、数字、点、下划线或短横线")
    manifest = Path(backup_manifest).expanduser().resolve()
    if not manifest.is_file():
        raise RuntimeError(f"备份 manifest 不存在，已停止：{manifest}")
    if not confirm_remove_internal:
        raise RuntimeError("migrate --apply 必须显式提供 --confirm-remove-internal")

    conn = await open_connection()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            before = await summarize(cur, "before")
            if before["internal_member_count"] != expected_internal_members:
                raise RuntimeError(
                    "内勤人员数量不符合预期，已停止："
                    f"expected={expected_internal_members}, "
                    f"actual={before['internal_member_count']}"
                )
            if before["unmapped_members"]:
                names = "、".join(item["name"] for item in before["unmapped_members"])
                raise RuntimeError(f"存在未配置明确归位的内勤人员，已停止：{names}")
            if before["shared_department_links"]:
                details = "、".join(
                    f"{item['name']}→{item['department']}"
                    for item in before["shared_department_links"]
                )
                raise RuntimeError(f"发现共享部门引用，已停止：{details}")
            if before["child_references"]:
                details = "、".join(
                    f"{item['name']}←{item['parent']}"
                    for item in before["child_references"]
                )
                raise RuntimeError(f"历史内勤部门仍有下级引用，已停止：{details}")
            resolved = await _resolve_targets(cur, before["internal_members"])
            internal_ids = [item["id"] for item in before["internal_departments"]]
            for member in before["internal_members"]:
                target_id = resolved[member["id"]]
                await cur.execute(
                    "DELETE FROM _grid_member_department_links WHERE member_id=%s",
                    (member["id"],),
                )
                community = ""
                if target_id is not None:
                    await cur.execute(
                        "INSERT INTO _grid_member_department_links "
                        "(member_id,department_id,sort_order) VALUES (%s,%s,0)",
                        (member["id"], target_id),
                    )
                    await cur.execute(
                        "SELECT community.name FROM _departments AS department "
                        "LEFT JOIN _communities AS community "
                        "ON community.id=department.community_id "
                        "WHERE department.id=%s",
                        (target_id,),
                    )
                    row = await cur.fetchone()
                    community = str(row[0] or "") if row else ""
                await cur.execute(
                    "UPDATE _grid_members SET department_id=%s, community=%s WHERE id=%s",
                    (target_id, community, member["id"]),
                )
            if internal_ids:
                placeholders = ",".join(["%s"] * len(internal_ids))
                await cur.execute(
                    f"SELECT COUNT(*) FROM _grid_member_department_links "
                    f"WHERE department_id IN ({placeholders})",
                    tuple(internal_ids),
                )
                if int((await cur.fetchone())[0] or 0):
                    raise RuntimeError("迁移后仍有内勤部门关系，已回滚")
                await cur.execute(
                    f"SELECT COUNT(*) FROM _grid_members "
                    f"WHERE department_id IN ({placeholders})",
                    tuple(internal_ids),
                )
                if int((await cur.fetchone())[0] or 0):
                    raise RuntimeError("迁移后仍有人员主部门指向内勤，已回滚")
                await cur.execute(
                    f"DELETE FROM _departments WHERE id IN ({placeholders}) "
                    "AND department_type='internal'",
                    tuple(internal_ids),
                )
            after = await summarize(cur, "after")
        await conn.commit()
        return {
            "command": "migrate",
            "run_id": run_id,
            "backup_manifest": str(manifest),
            "before": before,
            "after": after,
            "migrated_member_count": len(resolved),
            "external_tencent_materials": "untouched",
        }
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


async def run_verify() -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await summarize(cur, "verify")
            await cur.execute(
                "SELECT COUNT(*) FROM _grid_member_department_links AS link "
                "LEFT JOIN _departments AS department ON department.id=link.department_id "
                "WHERE department.id IS NULL"
            )
            orphan_links = int((await cur.fetchone())[0] or 0)
            await cur.execute(
                "SELECT COUNT(*) FROM _grid_members AS member "
                "LEFT JOIN _departments AS department ON department.id=member.department_id "
                "WHERE member.department_id IS NOT NULL AND department.id IS NULL"
            )
            orphan_primary = int((await cur.fetchone())[0] or 0)
        await conn.rollback()
    finally:
        await close_connection(conn)
    checks = {
        "internal_departments_empty": not result["internal_departments"],
        "internal_members_empty": result["internal_member_count"] == 0,
        "orphan_department_links_empty": orphan_links == 0,
        "orphan_primary_departments_empty": orphan_primary == 0,
    }
    return {
        **result,
        "orphan_department_links": orphan_links,
        "orphan_primary_departments": orphan_primary,
        "checks": checks,
        "ok": all(checks.values()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读列出历史内勤部门和人员")
    migrate = sub.add_parser("migrate", help="预览或执行内勤归位")
    migrate.add_argument("--apply", action="store_true", help="明确授权写入 PlatformData")
    migrate.add_argument("--run-id", help="维护运行编号")
    migrate.add_argument("--backup-manifest", help="执行前备份 manifest 的绝对或相对路径")
    migrate.add_argument("--expect-internal-members", type=int, help="要求内勤人员数量精确匹配")
    migrate.add_argument("--confirm-remove-internal", action="store_true", help="确认删除已清空的历史 internal 部门")
    sub.add_parser("verify", help="只读核验内勤部门和引用已清理")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await run_measure()
    elif args.command == "verify":
        result = await run_verify()
    elif not args.apply:
        result = await run_measure()
        result["command"] = "migrate-preview"
    else:
        missing = [
            name for name, value in (
                ("--run-id", args.run_id),
                ("--backup-manifest", args.backup_manifest),
                ("--expect-internal-members", args.expect_internal_members),
            ) if value is None
        ]
        if missing:
            raise RuntimeError("migrate --apply 缺少必填参数：" + ", ".join(missing))
        result = await run_apply(
            run_id=args.run_id,
            backup_manifest=args.backup_manifest,
            expected_internal_members=args.expect_internal_members,
            confirm_remove_internal=args.confirm_remove_internal,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
