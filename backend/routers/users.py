"""用户账号、人员关联和权限组分配。"""

from __future__ import annotations

from typing import Literal, Optional
import json

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import db_manager
from deps import require_super_admin
from services.audit import record_admin_audit, request_audit_fields


router = APIRouter(prefix="/api/users", tags=["用户管理"])


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    member_id: Optional[int] = Field(default=None, gt=0)
    assignment_mode: Literal["inherited", "custom"] = "inherited"
    permission_group_id: Optional[int] = Field(default=None, gt=0)
    permission_group_ids: Optional[list[int]] = Field(default=None, max_length=20)
    password_is_temporary: bool = True


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    member_id: Optional[int] = Field(default=None, gt=0)
    assignment_mode: Optional[Literal["inherited", "custom"]] = None
    permission_group_id: Optional[int] = Field(default=None, gt=0)
    permission_group_ids: Optional[list[int]] = Field(default=None, max_length=20)
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)
    password_is_temporary: Optional[bool] = None


def _legacy_role(group_codes: set[str]) -> str:
    if "super_admin" in group_codes:
        return "super_admin"
    if group_codes & {"admin", "internal_business"}:
        return "admin"
    if "global_viewer" in group_codes:
        return "leader"
    return "member"


async def _resolve_groups(
    cur,
    *,
    member_id: int | None,
    assignment_mode: str,
    permission_group_id: int | None,
    permission_group_ids: list[int] | None = None,
) -> list[dict]:
    if assignment_mode == "inherited":
        if member_id is None:
            raise HTTPException(400, "继承岗位权限时必须关联人员")
        await cur.execute(
            """
            SELECT DISTINCT permission_group.id, permission_group.code,
                   permission_group.name, permission_group.sort_order
            FROM _grid_members AS member
            LEFT JOIN _position_permission_group_links AS mapping
              ON mapping.position=member.position
            LEFT JOIN _permission_groups AS permission_group
              ON permission_group.id=mapping.permission_group_id
            WHERE member.id=%s
            ORDER BY permission_group.sort_order, permission_group.id
            """,
            (member_id,),
        )
        rows = await cur.fetchall()
        if not rows or rows[0][0] is None:
            raise HTTPException(400, "关联人员不存在或岗位尚未配置权限组")
    else:
        selected_ids = permission_group_ids
        if selected_ids is None and permission_group_id is not None:
            selected_ids = [permission_group_id]
        selected_ids = list(dict.fromkeys(int(value) for value in selected_ids or []))
        if not selected_ids or any(value <= 0 for value in selected_ids):
            raise HTTPException(400, "自定义权限时必须选择权限组")
        placeholders = ", ".join(["%s"] * len(selected_ids))
        await cur.execute(
            f"SELECT id, code, name, sort_order FROM _permission_groups "
            f"WHERE id IN ({placeholders}) "
            f"ORDER BY sort_order, id",
            selected_ids,
        )
        rows = await cur.fetchall()
        if len(rows) != len(selected_ids):
            raise HTTPException(400, "包含不存在的权限组")
    groups = [
        {"id": int(row[0]), "code": str(row[1]), "name": str(row[2])}
        for row in rows
    ]
    super_groups = [group for group in groups if group["code"] == "super_admin"]
    if super_groups and len(groups) != 1:
        raise HTTPException(400, "超级管理员组不能与其他权限组同时选择")
    return groups


async def _replace_custom_group_links(
    cur,
    user_id: int,
    assignment_mode: str,
    group_ids: list[int],
) -> None:
    await cur.execute(
        "DELETE FROM _user_permission_group_links WHERE user_id=%s",
        (user_id,),
    )
    if assignment_mode == "custom":
        await cur.executemany(
            "INSERT INTO _user_permission_group_links "
            "(user_id, permission_group_id) VALUES (%s, %s)",
            [(user_id, group_id) for group_id in group_ids],
        )


async def _ensure_member_available(cur, member_id: int | None, user_id=None):
    if member_id is None:
        return
    sql = "SELECT id FROM _users WHERE member_id=%s"
    params: list[object] = [member_id]
    if user_id is not None:
        sql += " AND id<>%s"
        params.append(user_id)
    await cur.execute(sql, params)
    if await cur.fetchone():
        raise HTTPException(409, "该人员已经关联其他账号")


async def _count_super_admins(cur) -> int:
    await cur.execute(
        """
        SELECT COUNT(*)
        FROM _users AS user
        JOIN _permission_groups AS permission_group
          ON permission_group.id=user.permission_group_id
        WHERE permission_group.code='super_admin'
        """
    )
    return int((await cur.fetchone())[0] or 0)


@router.get("")
async def list_users(user: dict = Depends(require_super_admin)):
    del user
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user.id, user.username, user.display_name,
                       user.role, user.member_id,
                       user.group_assignment_mode,
                       user.password_is_temporary,
                       user.created_at, user.updated_at,
                       member.name, member.position,
                       department.name, department.department_type,
                       permission_group.id, permission_group.code,
                       permission_group.name
                FROM _users AS user
                LEFT JOIN _grid_members AS member ON member.id=user.member_id
                LEFT JOIN _departments AS department
                  ON department.id=member.department_id
                LEFT JOIN _permission_groups AS permission_group
                  ON permission_group.id=user.permission_group_id
                ORDER BY user.id
                """
            )
            rows = await cur.fetchall()
            await cur.execute(
                """
                SELECT assignment.user_id, permission_group.id,
                       permission_group.code, permission_group.name
                FROM (
                    SELECT user.id AS user_id, link.permission_group_id
                    FROM _users AS user
                    JOIN _user_permission_group_links AS link
                      ON link.user_id=user.id
                    WHERE user.group_assignment_mode='custom'
                    UNION
                    SELECT user.id AS user_id, link.permission_group_id
                    FROM _users AS user
                    JOIN _grid_members AS member ON member.id=user.member_id
                    JOIN _position_permission_group_links AS link
                      ON link.position=member.position
                    WHERE user.group_assignment_mode='inherited'
                ) AS assignment
                JOIN _permission_groups AS permission_group
                  ON permission_group.id=assignment.permission_group_id
                ORDER BY assignment.user_id, permission_group.sort_order,
                         permission_group.id
                """
            )
            group_rows = await cur.fetchall()
    finally:
        pool.release(conn)
    groups_by_user: dict[int, list[dict]] = {}
    for linked_user_id, group_id, group_code, group_name in group_rows:
        groups_by_user.setdefault(int(linked_user_id), []).append({
            "id": int(group_id),
            "code": str(group_code),
            "name": str(group_name),
        })
    return {"data": [
        {
            "id": row[0],
            "username": row[1],
            "display_name": str(row[2] or row[9] or row[1]),
            "role": row[3],
            "member_id": row[4],
            "assignment_mode": row[5],
            "password_is_temporary": bool(row[6]),
            "created_at": str(row[7]) if row[7] else None,
            "updated_at": str(row[8]) if row[8] else None,
            "member": (
                {
                    "id": row[4], "name": row[9], "position": row[10],
                    "department_name": row[11], "department_type": row[12],
                }
                if row[4] is not None else None
            ),
            "permission_groups": groups_by_user.get(int(row[0])) or (
                [{"id": row[13], "code": row[14], "name": row[15]}]
                if row[13] is not None else []
            ),
            "permission_group": (
                (groups_by_user.get(int(row[0])) or [{
                    "id": row[13], "code": row[14], "name": row[15],
                }])[0]
                if groups_by_user.get(int(row[0])) or row[13] is not None
                else None
            ),
        }
        for row in rows
    ]}


@router.post("")
async def create_user(
    req: CreateUserRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    username = req.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    display_name = req.display_name.strip()
    if not display_name:
        raise HTTPException(400, "姓名不能为空")
    password_hash = bcrypt.hashpw(
        req.password.encode(), bcrypt.gensalt()
    ).decode()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await _ensure_member_available(cur, req.member_id)
            groups = await _resolve_groups(
                cur,
                member_id=req.member_id,
                assignment_mode=req.assignment_mode,
                permission_group_id=req.permission_group_id,
                permission_group_ids=req.permission_group_ids,
            )
            group_ids = [group["id"] for group in groups]
            group_codes = {group["code"] for group in groups}
            try:
                await cur.execute(
                    """
                    INSERT INTO _users (
                        username, display_name, password_hash, role, member_id,
                        permission_group_id, group_assignment_mode,
                        password_is_temporary
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        username, display_name, password_hash, _legacy_role(group_codes),
                        req.member_id, group_ids[0], req.assignment_mode,
                        int(req.password_is_temporary),
                    ),
                )
                created_user_id = int(cur.lastrowid)
                await _replace_custom_group_links(
                    cur,
                    created_user_id,
                    req.assignment_mode,
                    group_ids,
                )
                await cur.execute(
                    "INSERT INTO _permission_change_log "
                    "(action, target_type, target_id, detail, changed_by) "
                    "VALUES ('assign', 'user', %s, %s, %s)",
                    (
                        str(created_user_id),
                        json.dumps({
                            "member_id": req.member_id,
                            "assignment_mode": req.assignment_mode,
                            "permission_group_ids": group_ids,
                        }, ensure_ascii=False),
                        user["id"],
                    ),
                )
            except Exception as exc:
                if "Duplicate" in str(exc):
                    raise HTTPException(409, "用户名已存在") from exc
                raise
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    await record_admin_audit(
        user,
        "user.create",
        target_type="user",
        target_name=username,
        detail={
            "member_id": req.member_id,
            "assignment_mode": req.assignment_mode,
            "permission_groups": [group["name"] for group in groups],
            "temporary_password": req.password_is_temporary,
        },
        **request_audit_fields(request),
    )
    return {"message": "用户创建成功", "username": username}


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    req: UpdateUserRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user.member_id, user.group_assignment_mode,
                       user.permission_group_id, permission_group.code
                FROM _users AS user
                LEFT JOIN _permission_groups AS permission_group
                  ON permission_group.id=user.permission_group_id
                WHERE user.id=%s FOR UPDATE
                """,
                (user_id,),
            )
            existing = await cur.fetchone()
            if not existing:
                raise HTTPException(404, "用户不存在")
            await cur.execute(
                "SELECT permission_group_id "
                "FROM _user_permission_group_links WHERE user_id=%s",
                (user_id,),
            )
            existing_custom_group_ids = [
                int(row[0]) for row in await cur.fetchall()
            ]
            fields = req.model_fields_set
            member_id = req.member_id if "member_id" in fields else existing[0]
            assignment_mode = (
                req.assignment_mode
                if "assignment_mode" in fields
                else str(existing[1] or "inherited")
            )
            if "permission_group_ids" in fields:
                requested_group_ids = req.permission_group_ids
            elif "permission_group_id" in fields:
                requested_group_ids = (
                    [req.permission_group_id]
                    if req.permission_group_id is not None
                    else []
                )
            else:
                requested_group_ids = (
                    existing_custom_group_ids
                    or ([int(existing[2])] if existing[2] is not None else [])
                )
            await _ensure_member_available(cur, member_id, user_id)
            groups = await _resolve_groups(
                cur,
                member_id=member_id,
                assignment_mode=assignment_mode,
                permission_group_id=None,
                permission_group_ids=requested_group_ids,
            )
            group_ids = [group["id"] for group in groups]
            group_codes = {group["code"] for group in groups}
            if existing[3] == "super_admin" and "super_admin" not in group_codes:
                if await _count_super_admins(cur) <= 1:
                    raise HTTPException(409, "必须至少保留一个超级管理员")

            updates = {
                "member_id": member_id,
                "group_assignment_mode": assignment_mode,
                "permission_group_id": group_ids[0],
                "role": _legacy_role(group_codes),
            }
            if req.display_name is not None:
                display_name = req.display_name.strip()
                if not display_name:
                    raise HTTPException(400, "姓名不能为空")
                updates["display_name"] = display_name
            if req.password is not None:
                updates["password_hash"] = bcrypt.hashpw(
                    req.password.encode(), bcrypt.gensalt()
                ).decode()
            if req.password_is_temporary is not None:
                updates["password_is_temporary"] = int(
                    req.password_is_temporary
                )
            set_clause = ", ".join(f"{key}=%s" for key in updates)
            await cur.execute(
                f"UPDATE _users SET {set_clause} WHERE id=%s",
                [*updates.values(), user_id],
            )
            await _replace_custom_group_links(
                cur,
                user_id,
                assignment_mode,
                group_ids,
            )
            await cur.execute(
                "INSERT INTO _permission_change_log "
                "(action, target_type, target_id, detail, changed_by) "
                "VALUES ('assign', 'user', %s, %s, %s)",
                (
                    str(user_id),
                    json.dumps({
                        "member_id": member_id,
                        "assignment_mode": assignment_mode,
                        "permission_group_ids": group_ids,
                    }, ensure_ascii=False),
                    user["id"],
                ),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    await record_admin_audit(
        user,
        "user.update",
        target_type="user",
        target_name=str(user_id),
        detail={
            "member_id": member_id,
            "assignment_mode": assignment_mode,
            "permission_groups": [group["name"] for group in groups],
            "password_changed": req.password is not None,
        },
        **request_audit_fields(request),
    )
    return {"message": "用户修改成功"}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
):
    if int(user["id"]) == user_id:
        raise HTTPException(400, "不能删除自己")
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT permission_group.code
                FROM _users AS user
                LEFT JOIN _permission_groups AS permission_group
                  ON permission_group.id=user.permission_group_id
                WHERE user.id=%s FOR UPDATE
                """,
                (user_id,),
            )
            existing = await cur.fetchone()
            if not existing:
                raise HTTPException(404, "用户不存在")
            if existing[0] == "super_admin" and await _count_super_admins(cur) <= 1:
                raise HTTPException(409, "必须至少保留一个超级管理员")
            await cur.execute("DELETE FROM _sessions WHERE user_id=%s", (user_id,))
            await cur.execute("DELETE FROM _notifications WHERE user_id=%s", (user_id,))
            await cur.execute(
                "DELETE FROM _announcement_reads WHERE user_id=%s",
                (user_id,),
            )
            await cur.execute(
                "DELETE FROM _user_permission_group_links WHERE user_id=%s",
                (user_id,),
            )
            await cur.execute("DELETE FROM _users WHERE id=%s", (user_id,))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    await record_admin_audit(
        user,
        "user.delete",
        target_type="user",
        target_name=str(user_id),
        **request_audit_fields(request),
    )
    return {"message": "用户已删除"}
