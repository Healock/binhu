"""超级管理员权限组和岗位默认组管理。"""

from __future__ import annotations

import secrets
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import (
    ALL_PERMISSIONS,
    POSITION_DEFAULT_GROUP,
    catalog_payload,
    parse_permissions,
    serialize_permissions,
)


router = APIRouter(prefix="/api/permission-groups", tags=["权限组管理"])


class PermissionGroupPayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[str]
    data_scope: str


class PositionMappingsPayload(BaseModel):
    mappings: dict[str, int | list[int]]


def _normalize_mapping_values(
    mappings: dict[str, int | list[int]],
) -> dict[str, list[int]]:
    normalized: dict[str, list[int]] = {}
    for position, raw_value in mappings.items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        group_ids = list(dict.fromkeys(int(value) for value in values))
        if not group_ids or any(value <= 0 for value in group_ids):
            raise HTTPException(400, f"岗位“{position}”至少选择一个权限组")
        normalized[position] = group_ids
    return normalized


async def _effective_group_user_counts(cur) -> dict[int, int]:
    await cur.execute(
        """
        SELECT assignment.permission_group_id,
               COUNT(DISTINCT assignment.user_id)
        FROM (
            SELECT link.permission_group_id, user.id AS user_id
            FROM _user_permission_group_links AS link
            JOIN _users AS user ON user.id=link.user_id
            WHERE user.group_assignment_mode='custom'
            UNION ALL
            SELECT link.permission_group_id, user.id AS user_id
            FROM _position_permission_group_links AS link
            JOIN _grid_members AS member ON member.position=link.position
            JOIN _users AS user ON user.member_id=member.id
            WHERE user.group_assignment_mode='inherited'
        ) AS assignment
        GROUP BY assignment.permission_group_id
        """
    )
    return {int(row[0]): int(row[1]) for row in await cur.fetchall()}


def _normalize_payload(data: PermissionGroupPayload) -> tuple[str, str, list[str]]:
    name = data.name.strip()
    if data.data_scope not in {"all", "own_department"}:
        raise HTTPException(400, "数据范围只能是全部或所属部门")
    unknown = sorted(set(data.permissions) - ALL_PERMISSIONS)
    if unknown:
        raise HTTPException(400, f"包含未知权限：{', '.join(unknown)}")
    permissions = parse_permissions(data.permissions)
    return name, data.data_scope, permissions


async def _record_change(cur, action: str, target_type: str, target_id, detail, user_id):
    await cur.execute(
        "INSERT INTO _permission_change_log "
        "(action, target_type, target_id, detail, changed_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            action,
            target_type,
            str(target_id),
            json.dumps(detail, ensure_ascii=False),
            user_id,
        ),
    )


@router.get("/catalog")
async def permission_catalog(user: dict = Depends(require_super_admin)):
    del user
    return {
        "permissions": catalog_payload(),
        "data_scopes": [
            {"value": "own_department", "label": "所属社区"},
            {"value": "all", "label": "全部社区"},
        ],
    }


@router.get("/departments")
async def departments(
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, name, department_type, community_id "
            "FROM _departments WHERE is_active=1 "
            "ORDER BY department_type, name"
        )
        rows = await cur.fetchall()
    return {
        "data": [
            {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "community_id": row[3],
            }
            for row in rows
        ]
    }


@router.get("")
async def list_groups(
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT permission_group.id, permission_group.code,
                   permission_group.name, permission_group.description,
                   permission_group.permissions,
                   permission_group.data_scope,
                   permission_group.is_system, permission_group.is_locked,
                   permission_group.sort_order
            FROM _permission_groups AS permission_group
            ORDER BY permission_group.sort_order, permission_group.id
        """)
        rows = await cur.fetchall()
        await cur.execute("""
            SELECT mapping.position, mapping.permission_group_id
            FROM _position_permission_group_links AS mapping
            JOIN _permission_groups AS permission_group
              ON permission_group.id=mapping.permission_group_id
            ORDER BY mapping.position, permission_group.sort_order,
                     permission_group.id
        """)
        mapping_rows = await cur.fetchall()
        user_counts = await _effective_group_user_counts(cur)
        await cur.execute(
            """
            SELECT member.position, COUNT(DISTINCT user.id)
            FROM _users AS user
            JOIN _grid_members AS member ON member.id=user.member_id
            WHERE user.group_assignment_mode='inherited'
            GROUP BY member.position
            """
        )
        position_user_counts = {
            str(row[0]): int(row[1]) for row in await cur.fetchall()
        }
    positions: dict[int, list[str]] = {}
    for position, group_id in mapping_rows:
        positions.setdefault(int(group_id), []).append(str(position))
    return {
        "data": [
            {
                "id": row[0],
                "code": row[1],
                "name": row[2],
                "description": row[3],
                "permissions": parse_permissions(row[4]),
                "data_scope": row[5],
                "is_system": bool(row[6]),
                "is_locked": bool(row[7]),
                "sort_order": row[8],
                "user_count": user_counts.get(int(row[0]), 0),
                "positions": positions.get(int(row[0]), []),
            }
            for row in rows
        ],
        "position_mappings": {
            position: [
                int(group_id)
                for mapped_position, group_id in mapping_rows
                if str(mapped_position) == position
            ]
            for position in sorted({str(row[0]) for row in mapping_rows})
        },
        "position_user_counts": position_user_counts,
    }


@router.post("")
async def create_group(
    data: PermissionGroupPayload,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    name, data_scope, permissions = _normalize_payload(data)
    code = f"custom_{secrets.token_hex(6)}"
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO _permission_groups "
                "(code, name, description, permissions, data_scope, "
                "is_system, is_locked, sort_order) "
                "VALUES (%s, %s, %s, %s, %s, 0, 0, 100)",
                (
                    code,
                    name,
                    data.description.strip(),
                    serialize_permissions(permissions),
                    data_scope,
                ),
            )
            group_id = cur.lastrowid
            await _record_change(
                cur, "create", "permission_group", group_id,
                {"name": name, "permissions": permissions}, user["id"],
            )
    except Exception as exc:
        if "Duplicate" in str(exc):
            raise HTTPException(400, "权限组名称已存在") from exc
        raise
    await record_admin_audit(
        user,
        "permission_group.create",
        target_type="permission_group",
        target_name=str(group_id),
        detail={"name": name, "permission_count": len(permissions)},
        **request_audit_fields(request),
    )
    return {"id": group_id, "code": code, "message": "权限组已创建"}


@router.put("/groups/{group_id}")
async def update_group(
    group_id: int,
    data: PermissionGroupPayload,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    name, data_scope, permissions = _normalize_payload(data)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT code, is_system, is_locked FROM _permission_groups "
            "WHERE id=%s",
            (group_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "权限组不存在")
        if row[2] or row[0] == "super_admin":
            raise HTTPException(400, "超级管理员权限组不能修改")
        saved_name = str(row[0]) if row[1] else name
        if row[1]:
            await cur.execute(
                "SELECT name FROM _permission_groups WHERE id=%s",
                (group_id,),
            )
            saved_name = str((await cur.fetchone())[0])
        try:
            await cur.execute(
                "UPDATE _permission_groups SET name=%s, description=%s, "
                "permissions=%s, data_scope=%s WHERE id=%s",
                (
                    saved_name,
                    data.description.strip(),
                    serialize_permissions(permissions),
                    data_scope,
                    group_id,
                ),
            )
        except Exception as exc:
            if "Duplicate" in str(exc):
                raise HTTPException(400, "权限组名称已存在") from exc
            raise
        affected_users = (await _effective_group_user_counts(cur)).get(
            group_id,
            0,
        )
        await _record_change(
            cur, "update", "permission_group", group_id,
            {"permissions": permissions, "data_scope": data_scope}, user["id"],
        )
    await record_admin_audit(
        user,
        "permission_group.update",
        target_type="permission_group",
        target_name=str(group_id),
        detail={
            "permission_count": len(permissions),
            "affected_users": affected_users,
        },
        **request_audit_fields(request),
    )
    return {"message": "权限组已保存", "affected_users": affected_users}


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT is_system FROM _permission_groups WHERE id=%s",
            (group_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "权限组不存在")
        if row[0]:
            raise HTTPException(400, "预设权限组不能删除")
        if (await _effective_group_user_counts(cur)).get(group_id, 0):
            raise HTTPException(400, "仍有账号使用该权限组")
        await cur.execute(
            "SELECT COUNT(*) FROM _position_permission_group_links "
            "WHERE permission_group_id=%s",
            (group_id,),
        )
        if (await cur.fetchone())[0]:
            raise HTTPException(400, "仍有岗位使用该权限组")
        await cur.execute("DELETE FROM _permission_groups WHERE id=%s", (group_id,))
        await _record_change(
            cur, "delete", "permission_group", group_id, {}, user["id"],
        )
    await record_admin_audit(
        user,
        "permission_group.delete",
        target_type="permission_group",
        target_name=str(group_id),
        **request_audit_fields(request),
    )
    return {"message": "权限组已删除"}


@router.put("/position-mappings/all")
async def update_position_mappings(
    data: PositionMappingsPayload,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    expected = set(POSITION_DEFAULT_GROUP)
    if set(data.mappings) != expected:
        raise HTTPException(400, "必须为全部岗位设置默认权限组")
    normalized = _normalize_mapping_values(data.mappings)
    group_ids = sorted({
        group_id
        for values in normalized.values()
        for group_id in values
    })
    placeholders = ", ".join(["%s"] * len(group_ids))
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, code, sort_order FROM _permission_groups "
                f"WHERE id IN ({placeholders})",
                group_ids,
            )
            group_rows = await cur.fetchall()
            if len(group_rows) != len(group_ids):
                raise HTTPException(400, "岗位映射包含不存在的权限组")
            if any(str(row[1]) == "super_admin" for row in group_rows):
                raise HTTPException(400, "超级管理员组不能作为岗位默认权限")
            group_meta = {
                int(row[0]): {"code": str(row[1]), "sort_order": int(row[2])}
                for row in group_rows
            }
            affected_users = 0
            for position, selected_ids in normalized.items():
                selected_ids.sort(
                    key=lambda group_id: (
                        group_meta[group_id]["sort_order"],
                        group_id,
                    )
                )
                await cur.execute(
                    "DELETE FROM _position_permission_group_links "
                    "WHERE position=%s",
                    (position,),
                )
                await cur.executemany(
                    "INSERT INTO _position_permission_group_links "
                    "(position, permission_group_id, updated_by) "
                    "VALUES (%s, %s, %s)",
                    [
                        (position, group_id, user["id"])
                        for group_id in selected_ids
                    ],
                )
                primary_group_id = selected_ids[0]
                await cur.execute(
                    "INSERT INTO _position_permission_groups "
                    "(position, permission_group_id, updated_by) "
                    "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
                    "permission_group_id=VALUES(permission_group_id), "
                    "updated_by=VALUES(updated_by)",
                    (position, primary_group_id, user["id"]),
                )
                codes = {
                    group_meta[group_id]["code"] for group_id in selected_ids
                }
                legacy_role = (
                    "admin"
                    if codes & {"admin", "internal_business"}
                    else "leader"
                    if "global_viewer" in codes
                    else "member"
                )
                await cur.execute(
                    """
                    UPDATE _users AS account
                    JOIN _grid_members AS member
                      ON member.id=account.member_id
                    SET account.permission_group_id=%s,
                        account.role=CASE
                            WHEN account.role='super_admin' THEN account.role
                            ELSE %s
                        END
                    WHERE account.group_assignment_mode='inherited'
                      AND member.position=%s
                    """,
                    (primary_group_id, legacy_role, position),
                )
                affected_users += max(cur.rowcount, 0)
            await _record_change(
                cur, "update", "position_mapping", "all",
                {"mappings": normalized}, user["id"],
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "permission_group.position_mapping.update",
        target_type="permission_group",
        target_name="position_mappings",
        detail={"affected_users": affected_users},
        **request_audit_fields(request),
    )
    return {"message": "岗位默认权限已保存", "affected_users": affected_users}
